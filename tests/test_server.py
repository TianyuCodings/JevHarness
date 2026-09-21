from fastapi.testclient import TestClient
from auto_jev.server import create_app
from auto_jev.storage import RunStore


def test_dashboard_is_read_only_and_does_not_expose_holdout_or_files(tmp_path):
    store=RunStore(tmp_path/'runs');run=store.create_run('demo',{})
    store.save_trace(run,'holdout','candidate','episode',{'secret':'held out'})
    (tmp_path/'.env').write_text('PRIVATE_KEY=must-not-appear')
    client=TestClient(create_app(tmp_path/'runs'))
    assert client.get('/api/runs').status_code==200
    assert client.get(f'/api/runs/{run}/matrix?split=holdout').status_code==404
    assert client.get(f'/api/runs/{run}/trace/candidate/episode?split=holdout').status_code==400
    for url in ('/.env','/..%2f.env','/api/runs/..%2f.env'):
        response=client.get(url)
        assert response.status_code==404 and 'must-not-appear' not in response.text
    assert client.post('/api/runs',json={'name':'injected'}).status_code==405
