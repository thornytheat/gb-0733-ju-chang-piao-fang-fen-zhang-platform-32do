"""服务费结算单的归属规则。

渠道按整张套票出账，单笔金额不可拆改。整单金额只按该票的原分账权重摊到各个
演出场次；只有该票在该场次实际入场且场次未停演时，份额才落到场馆。其余份额
保留在「未落地」并带原因，绝不重新摊到其他场馆。
"""
from app.modules.m4_allocation.rules import split_cents

NO_RIGHTS = 'FEE_NO_RIGHTS'
CANCELLED = 'FEE_CANCELLED_SHOW'
AWAITING = 'FEE_AWAITING_SCAN'
UNLANDED_REASONS = (NO_RIGHTS, CANCELLED, AWAITING)


def fee_split(fee_cents, originals):
    """按原分账权重拆分整单服务费，沿用原分账的最大余数裁决。"""
    weights = {row['show']: row['weight'] for row in originals}
    return {show: share['allocated_cents'] for show, share in split_cents(fee_cents, weights)}


def landing(show_definition, attended):
    """场次份额是否落到场馆：停演优先，其次看是否实际入场。"""
    if show_definition['cancelled_at']:
        return False, CANCELLED
    if not attended:
        return False, AWAITING
    return True, ''
