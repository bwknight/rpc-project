import datetime
import pytest


@pytest.mark.parametrize('data', [
    None,
    True,
    False,
    datetime.datetime(1967, 5, 4, 3, 2, 1, tzinfo=datetime.timezone.utc),
    'string',
    b'bytes',
    123,
    0.1,
    [0, 1, 'abc'],
    {'key': 'value'},
    [{'key': 'value'}, [1, 2]]])
def test_serialization(client, data):
    assert client.cf(data).py == data
