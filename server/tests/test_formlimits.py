from fastapi import FastAPI, UploadFile
from fastapi.testclient import TestClient

from app import formlimits  # noqa: F401  (importing it lifts the 1000-file limit)


def test_more_than_a_thousand_files_are_accepted():
    app = FastAPI()

    @app.post("/n")
    async def n(files: list[UploadFile]):
        return {"n": len(files)}

    files = [("files", (f"p{i}.jpg", b"x", "image/jpeg")) for i in range(1100)]
    r = TestClient(app).post("/n", files=files)
    assert r.status_code == 200 and r.json() == {"n": 1100}
