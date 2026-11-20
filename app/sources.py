from app.config import ALL_SOURCES, SAMPLE_DIR, SOURCE_COLUMNS
from app.modules.m1_intake.reader import read_source

FEE_SOURCE = 'fees.csv'


def sample_sources():
    return {name: (SAMPLE_DIR / name).read_bytes() for name in SOURCE_COLUMNS}


def fee_sample_sources():
    """内置样例加一份服务费结算单，供场馆净应收与未落地演示。"""
    return {**sample_sources(), FEE_SOURCE: (SAMPLE_DIR / FEE_SOURCE).read_bytes()}


class SourceSet:
    def __init__(self, source_bytes, isolates):
        self.source_bytes = source_bytes
        self.isolates = isolates
        self.stats = {name: {} for name in ALL_SOURCES}

    def rows(self, name):
        if name not in self.source_bytes:
            self.stats[name].update(encoding='', status='ABSENT', rows=0, blank=0, headers=0)
            return iter(())
        return read_source(self.source_bytes[name], name, self.stats[name], self.isolates)
