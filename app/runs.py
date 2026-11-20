from typing import Annotated

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse

from app.config import ALL_SOURCES, MAX_SOURCE_BYTES, SAMPLE_BOOK, SOURCE_COLUMNS
from app.errors import Problem
from app.sources import fee_sample_sources, sample_sources

router = APIRouter(prefix='/api/runs')
Book = Annotated[str, Form(pattern=r'^[A-Za-z0-9][A-Za-z0-9_-]{0,39}$')]


def publish(request, book, sources):
    result, reused = request.app.state.store.commit(book, sources)
    return JSONResponse({'run_id': result['manifest']['run_id'], 'reused': reused, 'manifest': result['manifest'],
                         'totals': result['funding']['totals'], 'fee_totals': result['funding']['fee_totals'],
                         'isolated': len(result['quarantine'])},
                        status_code=200 if reused else 201)


@router.post('/sample')
def publish_sample(request: Request):
    return publish(request, SAMPLE_BOOK, sample_sources())


@router.post('/sample-fees')
def publish_fee_sample(request: Request):
    """内置样例加服务费结算单：演示按整张套票归属到场馆与未落地。"""
    return publish(request, SAMPLE_BOOK, fee_sample_sources())


@router.post('')
def publish_files(request: Request, book: Book, files: Annotated[list[UploadFile], File()]):
    names = [item.filename for item in files]
    if not set(SOURCE_COLUMNS) <= set(names) or not set(names) <= set(ALL_SOURCES) or len(names) != len(set(names)):
        raise Problem(422, 'SOURCE_SET', {'expected': list(SOURCE_COLUMNS), 'optional': ['fees.csv']})
    sources = {}
    for item in files:
        raw = item.file.read(MAX_SOURCE_BYTES + 1)
        if len(raw) > MAX_SOURCE_BYTES:
            raise Problem(413, 'FILE_LIMIT', {'source': item.filename})
        sources[item.filename] = raw
    return publish(request, book, sources)


@router.post('/{run_id}/rerun')
def rerun(request: Request, run_id: str):
    store = request.app.state.store
    snapshot = store.get(run_id)
    return publish(request, snapshot['manifest']['book'], store.inputs(run_id))
