"""«Чарты и жанры» (see app/charts.py): the list for the Home screen and a chart's tracks."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException

from app import charts
from app.api.schemas import ChartOut, ChartPageOut, ChartsOut, ChartTrackOut
from app.auth import current_user
from app.db import session_scope
from app.library.owned import annotate_tracks
from app.models import User

router = APIRouter(prefix="/api/charts", tags=["charts"])

COVERS = 4


def _out(c: charts.Chart, built: charts.Built | None) -> ChartOut:
    covers = [t.cover_url for t in (built.tracks if built else []) if t.cover_url]
    return ChartOut(id=c.id, title=c.title, name=c.name, kind=c.kind, group=c.group, source=c.source,
                    covers=list(dict.fromkeys(covers))[:COVERS],
                    updated_at=datetime.fromtimestamp(built.at, UTC) if built else None)


@router.get("", response_model=ChartsOut)
async def list_charts(user: User = Depends(current_user)):
    """Every chart and genre, the Home screen's rows in order (`group`); the covers of those made already."""
    return ChartsOut(charts=[_out(c, await charts.cached(c.id)) for c in charts.CHARTS.values()])


@router.get("/{chart_id}", response_model=ChartPageOut)
async def chart(chart_id: str, user: User = Depends(current_user)):
    """The chart's tracks in order. The first request of a chart may take up to a minute (its names are matched with
    the catalog); later ones answer at once. 502 when its source is down and no copy is kept."""
    c = charts.get(chart_id)
    if c is None:
        raise HTTPException(404, "Чарт не найден")
    try:
        built = await charts.tracks(chart_id)  # no DB connection held meanwhile
    except charts.ChartError as e:
        raise HTTPException(502, f"Чарт недоступен: {e}") from e
    async with session_scope() as s:
        annotated = await annotate_tracks(s, built.tracks, user.id)
    return ChartPageOut(chart=_out(c, built),
                        tracks=[ChartTrackOut(**t.model_dump(), position=p)
                                for t, p in zip(annotated, built.positions, strict=True)])
