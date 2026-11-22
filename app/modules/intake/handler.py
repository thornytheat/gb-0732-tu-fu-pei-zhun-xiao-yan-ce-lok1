from typing import Annotated

from fastapi import APIRouter, Path, Query, Request

from app.config import DATASETS, RunOptions
from app.errors import RegisterError
from app.modules.intake.service import receive_intake
from app.pipeline import Survey

router = APIRouter(prefix='/api/intake')
IntakeId = Annotated[str, Path(pattern=r'^[0-9a-f]{64}$')]


@router.get('')
def inspect(request: Request, options: Annotated[RunOptions, Query()]):
    survey = Survey(request.app.state.settings, options)
    snapshot, reused = receive_intake(request.app.state.store, options.dataset, survey)
    return {'reused': reused, **snapshot}


@router.get('/batches')
def batches(request: Request, dataset: str):
    if dataset not in DATASETS:
        raise RegisterError('UNKNOWN_DATASET', '请选择已登记的图册', 404)
    return {'batches': request.app.state.store.list_intakes(dataset)}


@router.get('/batches/{intake_id}')
def batch(request: Request, intake_id: IntakeId):
    return request.app.state.store.get_intake(intake_id)
