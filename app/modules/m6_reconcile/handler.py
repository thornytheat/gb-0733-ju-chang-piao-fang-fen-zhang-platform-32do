from fastapi import APIRouter, Depends, Request

from app.browse import Browse, snapshot_for

router = APIRouter(prefix='/api/reconcile')


@router.get('/{run_id}')
def reconcile(request: Request, run_id: str, browse: Browse = Depends()):
    funding = snapshot_for(request, run_id)['funding']
    rows = browse.choose({'accepted': funding['ledger'], 'tickets': funding['tickets'], 'venues': funding['venues'],
                          'shows': funding['shows'], 'sources': funding['sources'],
                          'fee_venues': funding['fee_venues']})
    return browse.page(rows, totals=funding['totals'], fee_totals=funding['fee_totals'],
                       checks=funding['checks'], balanced=funding['balanced'])
