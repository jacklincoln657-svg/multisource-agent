"""
ModernDashboard：声明式 Canvas 构建器。

Agent 决定画什么、用哪些字段、几张图、标题；
本模块强制主题、边距、图例、响应式 HTML 壳（收回样式控制权）。
"""
from __future__ import annotations

import json
import math
import os
from typing import Any, Optional

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

COLORS = {
    "steam": "#2c7a7b",
    "spotify": "#1db954",
    "bilibili": "#fb7299",
    "fusion": "#dd6b20",
    "accent": "#805ad5",
    "bg": "#f4f6f8",
    "card": "#ffffff",
    "text": "#1a202c",
    "muted": "#718096",
}

_PALETTE = [
    COLORS["steam"],
    COLORS["spotify"],
    COLORS["bilibili"],
    COLORS["fusion"],
    COLORS["accent"],
    "#3182ce",
    "#d69e2e",
]


def _as_series(df: pd.DataFrame, col: str) -> pd.Series:
    if col not in df.columns:
        raise KeyError(f"column '{col}' not in dataframe columns={list(df.columns)}")
    return df[col]


def _grid(n: int) -> tuple[int, int]:
    if n <= 0:
        return 1, 1
    if n == 1:
        return 1, 1
    if n == 2:
        return 1, 2
    if n <= 4:
        return 2, 2
    if n <= 6:
        return 2, 3
    cols = 3
    rows = math.ceil(n / cols)
    return rows, cols


class ModernDashboard:
    """
    用法示例::

        board = ModernDashboard("Steam x Spotify", subtitle="Phase 1")
        board.add_bar(df_steam, x="hours", y="title", title="Steam hours", horizontal=True, color="steam")
        board.add_bar(df_tracks, x="play_count", y="track_name", title="Spotify plays", horizontal=True, color="spotify")
        board.add_bar_line(merged, x="title", y_bar="hours", y_line="user_rating", title="Fusion")
        board.add_pie(genre_df, names="main_genre", values="play_count", title="Genres")
        board.save("profile_dashboard.html")
    """

    def __init__(self, title: str = "Personal Profile Dashboard", subtitle: str = ""):
        self.title = title
        self.subtitle = subtitle
        self._panels: list[dict[str, Any]] = []

    def add_bar(
        self,
        df: pd.DataFrame,
        x: str,
        y: str,
        title: str = "",
        horizontal: bool = False,
        color: Optional[str] = None,
        name: Optional[str] = None,
    ) -> "ModernDashboard":
        self._panels.append(
            {
                "kind": "bar",
                "df": df.copy(),
                "x": x,
                "y": y,
                "title": title or name or "Bar",
                "horizontal": horizontal,
                "color": color,
                "name": name or title or "value",
            }
        )
        return self

    def add_line(
        self,
        df: pd.DataFrame,
        x: str,
        y: str,
        title: str = "",
        color: Optional[str] = None,
        name: Optional[str] = None,
    ) -> "ModernDashboard":
        self._panels.append(
            {
                "kind": "line",
                "df": df.copy(),
                "x": x,
                "y": y,
                "title": title or name or "Line",
                "color": color,
                "name": name or title or "value",
            }
        )
        return self

    def add_bar_line(
        self,
        df: pd.DataFrame,
        x: str,
        y_bar: str,
        y_line: str,
        title: str = "",
        bar_name: str = "bar",
        line_name: str = "line",
        bar_color: Optional[str] = None,
        line_color: Optional[str] = None,
    ) -> "ModernDashboard":
        """单面板双 Y 轴：柱状 + 折线（适合时长 vs 评分）。"""
        self._panels.append(
            {
                "kind": "bar_line",
                "df": df.copy(),
                "x": x,
                "y_bar": y_bar,
                "y_line": y_line,
                "title": title or "Bar + Line",
                "bar_name": bar_name,
                "line_name": line_name,
                "bar_color": bar_color or "steam",
                "line_color": line_color or "fusion",
            }
        )
        return self

    def add_pie(
        self,
        df: pd.DataFrame,
        names: str,
        values: str,
        title: str = "",
        hole: float = 0.45,
    ) -> "ModernDashboard":
        self._panels.append(
            {
                "kind": "pie",
                "df": df.copy(),
                "names": names,
                "values": values,
                "title": title or "Pie",
                "hole": hole,
            }
        )
        return self

    def add_radar(
        self,
        df: pd.DataFrame,
        categories: str,
        values: str,
        title: str = "",
        name: Optional[str] = None,
        color: Optional[str] = None,
    ) -> "ModernDashboard":
        self._panels.append(
            {
                "kind": "radar",
                "df": df.copy(),
                "categories": categories,
                "values": values,
                "title": title or "Radar",
                "name": name or title or "score",
                "color": color or "spotify",
            }
        )
        return self

    def _color(self, key: Optional[str], index: int = 0) -> str:
        if key and key in COLORS:
            return COLORS[key]
        if key and key.startswith("#"):
            return key
        return _PALETTE[index % len(_PALETTE)]

    def _build_figure(self) -> go.Figure:
        n = len(self._panels)
        if n == 0:
            raise ValueError("ModernDashboard has no panels; call add_bar / add_pie / ... first")

        rows, cols = _grid(n)
        specs: list[list[Optional[dict]]] = []
        titles: list[str] = []
        for i in range(rows * cols):
            r, c = divmod(i, cols)
            if len(specs) <= r:
                specs.append([None] * cols)
            if i < n:
                kind = self._panels[i]["kind"]
                if kind == "pie":
                    specs[r][c] = {"type": "domain"}
                elif kind == "radar":
                    specs[r][c] = {"type": "polar"}
                elif kind == "bar_line":
                    specs[r][c] = {"type": "xy", "secondary_y": True}
                else:
                    specs[r][c] = {"type": "xy"}
                titles.append(self._panels[i]["title"])
            else:
                specs[r][c] = None

        fig = make_subplots(
            rows=rows,
            cols=cols,
            specs=specs,
            subplot_titles=titles,
            vertical_spacing=0.12 if rows > 1 else 0.08,
            horizontal_spacing=0.10 if cols > 1 else 0.06,
        )

        for i, panel in enumerate(self._panels):
            r, c = divmod(i, cols)
            row, col = r + 1, c + 1
            kind = panel["kind"]
            df = panel["df"]

            if kind == "bar":
                color = self._color(panel.get("color"), i)
                if panel["horizontal"]:
                    fig.add_trace(
                        go.Bar(
                            y=_as_series(df, panel["y"]).astype(str),
                            x=_as_series(df, panel["x"]),
                            orientation="h",
                            name=panel["name"],
                            marker_color=color,
                            cliponaxis=False,
                            hovertemplate="%{y}: %{x}<extra></extra>",
                            showlegend=False,
                        ),
                        row=row,
                        col=col,
                    )
                else:
                    fig.add_trace(
                        go.Bar(
                            x=_as_series(df, panel["x"]).astype(str),
                            y=_as_series(df, panel["y"]),
                            name=panel["name"],
                            marker_color=color,
                            cliponaxis=False,
                            hovertemplate="%{x}: %{y}<extra></extra>",
                            showlegend=False,
                        ),
                        row=row,
                        col=col,
                    )
                fig.update_xaxes(showgrid=True, gridcolor="#e2e8f0", row=row, col=col)
                fig.update_yaxes(showgrid=False, row=row, col=col)

            elif kind == "line":
                color = self._color(panel.get("color"), i)
                fig.add_trace(
                    go.Scatter(
                        x=_as_series(df, panel["x"]).astype(str),
                        y=_as_series(df, panel["y"]),
                        mode="lines+markers",
                        name=panel["name"],
                        marker=dict(size=9, color=color),
                        line=dict(width=2, color=color),
                        hovertemplate="%{x}: %{y}<extra></extra>",
                        showlegend=False,
                    ),
                    row=row,
                    col=col,
                )

            elif kind == "bar_line":
                bc = self._color(panel.get("bar_color"), i)
                lc = self._color(panel.get("line_color"), i + 1)
                xs = _as_series(df, panel["x"]).astype(str)
                yb = _as_series(df, panel["y_bar"])
                yl = _as_series(df, panel["y_line"])
                fig.add_trace(
                    go.Bar(
                        x=xs,
                        y=yb,
                        name=panel["bar_name"],
                        marker_color=bc,
                        opacity=0.88,
                        hovertemplate="%{x}<br>" + panel["bar_name"] + ": %{y}<extra></extra>",
                    ),
                    row=row,
                    col=col,
                    secondary_y=False,
                )
                fig.add_trace(
                    go.Scatter(
                        x=xs,
                        y=yl,
                        name=panel["line_name"],
                        mode="lines+markers",
                        marker=dict(size=10, color=lc),
                        line=dict(width=2, color=lc),
                        hovertemplate="%{x}<br>" + panel["line_name"] + ": %{y}<extra></extra>",
                    ),
                    row=row,
                    col=col,
                    secondary_y=True,
                )
                ymax = float(yb.max()) if len(yb) else 1.0
                fig.update_yaxes(
                    title_text=panel["bar_name"],
                    range=[0, ymax * 1.25 if ymax > 0 else 1],
                    row=row,
                    col=col,
                    secondary_y=False,
                )
                fig.update_yaxes(
                    title_text=panel["line_name"],
                    range=[0, max(10.0, float(yl.max()) * 1.1 if len(yl) else 10.0)],
                    row=row,
                    col=col,
                    secondary_y=True,
                )
                fig.update_xaxes(tickangle=-25, row=row, col=col)

            elif kind == "pie":
                fig.add_trace(
                    go.Pie(
                        labels=_as_series(df, panel["names"]).astype(str),
                        values=_as_series(df, panel["values"]),
                        hole=panel.get("hole", 0.45),
                        marker=dict(colors=_PALETTE),
                        textinfo="label+percent",
                        hovertemplate="%{label}<br>%{value} (%{percent})<extra></extra>",
                        showlegend=False,
                    ),
                    row=row,
                    col=col,
                )

            elif kind == "radar":
                cats = list(_as_series(df, panel["categories"]).astype(str))
                vals = list(_as_series(df, panel["values"]).astype(float))
                if cats and vals:
                    cats = cats + [cats[0]]
                    vals = vals + [vals[0]]
                color = self._color(panel.get("color"), i)
                fig.add_trace(
                    go.Scatterpolar(
                        r=vals,
                        theta=cats,
                        fill="toself",
                        name=panel["name"],
                        line=dict(color=color, width=2),
                        fillcolor="rgba(29,185,84,0.18)",
                        hovertemplate="%{theta}: %{r}<extra></extra>",
                    ),
                    row=row,
                    col=col,
                )

        height = 420 if rows == 1 else 460 * rows
        width = 980 if cols == 1 else 1100
        fig.update_layout(
            title=dict(
                text=self.title,
                x=0.5,
                xanchor="center",
                font=dict(size=22, color=COLORS["text"], family="Segoe UI, system-ui, sans-serif"),
            ),
            height=height,
            width=width,
            paper_bgcolor=COLORS["bg"],
            plot_bgcolor=COLORS["card"],
            font=dict(family="Segoe UI, system-ui, sans-serif", color=COLORS["text"], size=12),
            margin=dict(t=90, b=56, l=48, r=40),
            legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0.5, xanchor="center"),
            bargap=0.35,
        )
        fig.update_annotations(font_size=13, font_color=COLORS["muted"])
        return fig

    def save(self, path: str) -> str:
        """写出带统一壳层的 HTML；plotly.js 走 CDN，避免数 MB 内嵌。"""
        fig = self._build_figure()
        plot_html = fig.to_html(
            full_html=False,
            include_plotlyjs="cdn",
            config={"displayModeBar": False, "responsive": True},
        )
        n = len(self._panels)
        sub = self.subtitle or f"{n} panels · ModernDashboard canvas"
        page = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>{self.title}</title>
<style>
  :root {{
    --bg: {COLORS["bg"]}; --card: {COLORS["card"]}; --text: {COLORS["text"]}; --muted: {COLORS["muted"]};
  }}
  * {{ box-sizing: border-box; }}
  body {{ margin:0; background:var(--bg); color:var(--text);
    font-family:"Segoe UI",system-ui,-apple-system,sans-serif; }}
  .wrap {{ max-width:1120px; margin:0 auto; padding:28px 16px 48px; }}
  header h1 {{ margin:0 0 6px; font-size:1.55rem; font-weight:650; letter-spacing:-0.02em; }}
  header p {{ margin:0; color:var(--muted); font-size:0.95rem; }}
  .panel {{
    margin-top:16px; background:var(--card); border:1px solid #e2e8f0; border-radius:14px;
    padding:8px; box-shadow:0 1px 2px rgba(16,24,40,.04); overflow:auto;
  }}
  .panel .plotly-graph-div {{ width:100% !important; }}
  footer {{ margin-top:12px; color:var(--muted); font-size:0.8rem; }}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>{self.title}</h1>
    <p>{sub}</p>
  </header>
  <div class="panel">{plot_html}</div>
  <footer>Rendered by ModernDashboard · styles locked · Agent chooses charts &amp; fields</footer>
</div>
</body>
</html>
"""
        out = os.path.abspath(path)
        os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
        with open(out, "w", encoding="utf-8") as f:
            f.write(page)
        return out


# --- Phase 1 数据辅助（可选，Agent 也可自己 pandas 解析）---

def _hours(minutes: float) -> float:
    return round(float(minutes) / 60.0, 1)


def load_steam(raw: dict[str, Any] | str) -> pd.DataFrame:
    data = json.loads(raw) if isinstance(raw, str) else raw
    games = data["response"]["games"]
    df = pd.DataFrame(games)
    df = df.rename(columns={"name": "title"})
    df["hours"] = df["playtime_forever"].map(_hours)
    return df[["title", "appid", "hours", "playtime_forever"]]


def load_spotify(raw: dict[str, Any] | str) -> tuple[pd.DataFrame, pd.DataFrame]:
    data = json.loads(raw) if isinstance(raw, str) else raw
    tracks = pd.DataFrame(data.get("top_tracks") or [])
    affinity = pd.DataFrame(data.get("game_affinity") or data.get("data") or [])
    if not affinity.empty and "title" not in affinity.columns and "track_name" in affinity.columns:
        affinity = affinity.rename(columns={"track_name": "title"})
    return tracks, affinity


def load_bilibili(raw: dict[str, Any] | str) -> tuple[pd.DataFrame, dict[str, Any]]:
    """观看历史 DataFrame + profile dict。可从 workspace/raw/bilibili_latest.json 读入。"""
    data = json.loads(raw) if isinstance(raw, str) else raw
    hist = pd.DataFrame(data.get("history") or [])
    if not hist.empty:
        if "title" not in hist.columns:
            hist["title"] = ""
        if "author_name" not in hist.columns:
            hist["author_name"] = ""
        if "view_at" not in hist.columns:
            hist["view_at"] = 0
        if "duration" not in hist.columns:
            hist["duration"] = 0
        if "progress" not in hist.columns:
            hist["progress"] = 0
    profile = data.get("profile") or {}
    return hist, profile


def build_merged(steam: pd.DataFrame, affinity: pd.DataFrame) -> pd.DataFrame:
    right = affinity.rename(columns={"main_genre": "affinity_genre"})
    return steam.merge(right, on="title", how="inner")
