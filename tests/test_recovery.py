from argparse import Namespace
from datetime import UTC, date, datetime, timedelta
import json
import numpy as np
import pandas as pd
import pytest
from scripts import fetch_open_meteo as f
from scripts import check_freshness as freshness


def frame(start, end, value=1.):
    d=pd.DataFrame({'timestamp_utc':pd.date_range(start,end+timedelta(days=1),freq='h',tz='UTC',inclusive='left')})
    for i in range(1,5):
        for var in ['temperature_2m','relative_humidity_2m','shortwave_radiation','wind_speed_100m_ms','wind_dir_sin','wind_dir_cos']:
            d[f'{var}_p{i}']=value
    return d


def args(start,end,phase='all'):
    return Namespace(countries=None,targets=None,start_date=start,end_date=end,
                     rolling_days=100,forecast_days=3,phase=phase,chunk_days=3)


def setup(tmp_path,monkeypatch,scopes=None):
    f.configure_data_dir(tmp_path/'data')
    config,fc=f.load_config()
    config['countries']=scopes or ['DE'];config['targets']=['solar']
    return config,fc


def test_history_timeout_does_not_discard_current_forecast(tmp_path,monkeypatch):
    cfg,fc=setup(tmp_path,monkeypatch);today=datetime.now(UTC).date()
    def fetch(country,target,start,end,config):
        if start<today:raise TimeoutError('archive unavailable')
        return frame(start,end)
    monkeypatch.setattr(f,'fetch_weather',fetch)
    status=f.collect(cfg,fc,args(today-timedelta(days=2),today+timedelta(days=1)))
    assert not status['complete']
    assert status['completed'][0]['phase']=='forecast'
    assert all(x['missing_hours']==0 for x in status['coverage'] if x['phase']=='forecast')
    assert pd.read_csv(f.MANIFEST_PATH).phase.tolist()==['forecast']
    assert json.loads((f.DATA_DIR/'status.json').read_text())['failures'][0]['phase']=='history'


def test_failed_country_does_not_block_other_countries(tmp_path,monkeypatch):
    cfg,fc=setup(tmp_path,monkeypatch,['BE','DE']);today=datetime.now(UTC).date()
    def fetch(country,target,start,end,config):
        if country=='BE':raise TimeoutError('failed request')
        return frame(start,end)
    monkeypatch.setattr(f,'fetch_weather',fetch)
    status=f.collect(cfg,fc,args(today,today,'forecast'))
    assert not status['complete']
    assert (f.RAW_DIR/'DE'/'solar'/f'{today.year}.csv').exists()
    assert pd.read_csv(f.MANIFEST_PATH).country.tolist()==['DE']


def test_history_resume_preserves_existing_vintage(tmp_path,monkeypatch):
    cfg,fc=setup(tmp_path,monkeypatch);today=datetime.now(UTC).date();day=today-timedelta(days=2)
    saved=frame(day,day,99.).iloc[:12];f.append_raw(saved,'DE','solar')
    monkeypatch.setattr(f,'fetch_weather',lambda c,t,s,e,fc:frame(s,e,1.))
    status=f.collect(cfg,fc,args(day,day,'history'));assert status['complete']
    d=pd.read_csv(f.RAW_DIR/'DE'/'solar'/f'{day.year}.csv')
    assert d.temperature_2m_p1.iloc[:12].eq(99.).all()
    assert d.temperature_2m_p1.iloc[12:].eq(1.).all()
    assert not list(f.history_windows('DE','solar',day,day,3))
    m=pd.read_csv(f.MANIFEST_PATH);assert m.vintage.iloc[0]=='retrospective_historical_forecast'
    assert m.path.iloc[0].startswith('data/updates/')


def test_small_missing_chunks_skip_good_days(tmp_path,monkeypatch):
    setup(tmp_path,monkeypatch);day=date(2026,10,1)
    f.append_raw(frame(day,day),'DE','solar')
    assert list(f.history_windows('DE','solar',day,day+timedelta(days=7),3))==[
      (date(2026,10,2),date(2026,10,4)),(date(2026,10,5),date(2026,10,7)),(date(2026,10,8),date(2026,10,8))]


def test_batched_locations_reject_incomplete_weather(monkeypatch):
    _,fc=f.load_config();day=date(2026,11,2)
    payload={'hourly':{'time':pd.date_range(day,periods=24,freq='h').astype(str).tolist(),
                         **{v:[1.]*24 for v in fc.hourly}}}
    payloads=[json.loads(json.dumps(payload)) for _ in range(4)]
    calls=[]
    monkeypatch.setattr(f,'request_json',lambda u,p,**kw:(calls.append(p) or payloads))
    points=f.selected_points('DE','solar')
    d=f.fetch_endpoint(url='test',points=points,start_date=day,end_date=day,config=fc)
    assert len(calls)==1 and len(calls[0]['latitude'].split(','))==4
    assert len(d)==24 and 'shortwave_radiation_p4' in d
    payloads[0]['hourly']['shortwave_radiation'][0]=None
    with pytest.raises(ValueError,match='non-finite'):
        f.fetch_endpoint(url='test',points=points,start_date=day,end_date=day,config=fc)


def test_freshness_requires_evening_retrieval_and_full_context(tmp_path,monkeypatch):
    cfg,fc=setup(tmp_path,monkeypatch)
    monkeypatch.setattr(f,'load_config',lambda:(cfg,fc))
    day=date(2026,11,2);f.append_raw(frame(day-timedelta(days=1),day+timedelta(days=1)),'DE','solar')
    f.append_manifest([dict(country='DE',target='solar',phase='forecast',collection_time_utc='2026-11-02T08:00:00Z',
                            run_id='x',path='data/x')])
    now=datetime(2026,11,2,17,tzinfo=UTC)
    assert not freshness.is_fresh(f.DATA_DIR,now=now,rolling_days=1,forecast_days=1)
    f.append_manifest([dict(country='DE',target='solar',phase='forecast',collection_time_utc='2026-11-02T16:00:00Z',
                            run_id='y',path='data/y')])
    assert freshness.is_fresh(f.DATA_DIR,now=now,rolling_days=1,forecast_days=1)
    assert not freshness.is_fresh(f.DATA_DIR,now=now,rolling_days=2,forecast_days=1)


def test_current_refresh_always_replaces_older_forecast(tmp_path,monkeypatch):
    setup(tmp_path,monkeypatch)
    day=date(2026,11,2)
    f.append_raw(frame(day,day+timedelta(days=3),10.),'DE','solar')
    f.append_raw(frame(day,day+timedelta(days=3),20.),'DE','solar')
    d=pd.read_csv(f.RAW_DIR/'DE'/'solar'/'2026.csv')
    assert len(d)==96 and d.temperature_2m_p1.eq(20.).all()


def test_stalled_batch_retries_individual_locations(monkeypatch):
    _,fc=f.load_config();day=date(2026,11,2);calls=[]
    def request(url,params,**kwargs):
        calls.append(params)
        if isinstance(params['latitude'],str) and ',' in params['latitude']:
            raise RuntimeError('batch timed out')
        return {'hourly':{'time':pd.date_range(day,periods=24,freq='h').astype(str).tolist(),
                          **{v:[params['latitude']]*24 for v in fc.hourly}}}
    monkeypatch.setattr(f,'request_json',request)
    d=f.fetch_endpoint(url='test',points=f.selected_points('DE','solar'),start_date=day,end_date=day,config=fc)
    assert len(calls)==5 and len(d)==24
    assert d.temperature_2m_p1.iloc[0]==51.44 and d.temperature_2m_p4.iloc[0]==49.8
