from fastapi import APIRouter, Depends, Request

from app.browse import Browse, isolated, snapshot_for

router = APIRouter(prefix='/api/fees')

VIEWS = ('settled', 'unlanded', 'venues', 'statements', 'isolated')


@router.get('/{run_id}')
def settlement(request: Request, run_id: str, browse: Browse = Depends()):
    snapshot = snapshot_for(request, run_id)
    fees = snapshot['fees']
    views = {
        'settled': [{**share, 'billed_at': line['billed_at'], 'billed_cents': line['billed_cents']}
                    for line in fees['lines'] for share in line['shares']],
        'unlanded': fees['unlanded'], 'venues': fees['venues'], 'statements': fees['statements'],
        'isolated': isolated(snapshot, 'fees.csv'),
    }
    return browse.page(browse.choose(views), totals=fees['totals'], checks=fees['checks'],
                       balanced=fees['balanced'], reason_cents=fees['reason_cents'])
