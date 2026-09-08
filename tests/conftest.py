import os
import uuid

import pytest
from dotenv import dotenv_values
from sqlalchemy import text
from sqlalchemy.engine import make_url

from readfellow.artifacts import make_engine
from readfellow.config import ReadFellowConfig
from readfellow.artifacts import open_artifacts


def pytest_addoption(parser):
    parser.addoption(
        "--mysql",
        action="store_true",
        help="run SQL tests in disposable MySQL databases",
    )


@pytest.fixture(autouse=True)
def artifacts(tmp_path, monkeypatch, request):
    admin = None
    database = None
    if request.config.getoption("--mysql"):
        uri = os.environ.get("MYSQL_URI") or dotenv_values(".env").get("MYSQL_URI")
        if not uri:
            pytest.fail("--mysql requires MYSQL_URI")
        admin = make_engine(uri)
        database = "readfellow_test_" + uuid.uuid4().hex
        with admin.begin() as connection:
            connection.execute(
                text(
                    f"CREATE DATABASE `{database}` CHARACTER SET utf8mb4 COLLATE utf8mb4_bin"
                )
            )
        uri = make_url(uri).set(database=database).render_as_string(hide_password=False)
    else:
        uri = f"sqlite:///{tmp_path / 'artifacts.sqlite'}"
    monkeypatch.setenv("MYSQL_URI", uri)
    store = open_artifacts(ReadFellowConfig())
    try:
        store.initialize()
        yield store
    finally:
        store.close()
        if admin is not None:
            with admin.begin() as connection:
                connection.execute(text(f"DROP DATABASE `{database}`"))
            admin.dispose()
