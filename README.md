# tuidash

A personal terminal dashboard built with [Textual](https://textual.textualize.io/) and [Rich](https://rich.readthedocs.io/).

> **This is a small side project.** It is hardwired to display the things that matter to me — my weather, my hosts, my portfolio, my feeds. There is no proper configuration system yet, and it is not designed to be easily customised by anyone else. Maybe one day it will grow into something more general. For now, it is just my dashboard.

![screenshot placeholder]

## What it shows

| Widget | Source |
|---|---|
| Clock | System time, pixel-art half-block font |
| Weather | [Open-Meteo](https://open-meteo.com/) — current conditions + 6-day forecast |
| Calendar | Month view with public-holiday highlighting via ICS feed |
| Portfolio | [Ghostfolio](https://ghostfolio.app/) — net worth, holdings, recent trades |
| Connectivity | Ping reachability, DNS resolution, download/upload speed via [Speedtest Tracker](https://github.com/alexjustesen/speedtest-tracker) |
| Hosts | Per-host ping, CPU, RAM and Docker container health via [Glances](https://github.com/nicolargo/glances) |
| News | RSS feeds with horizontal marquee scrolling |

## Running

```bash
uv run tuidash            # terminal
uv run tuidash --serve    # browser at http://localhost:8080
uv run tuidash --serve --port 9000
```

## Requirements

- Python 3.13+
- [uv](https://docs.astral.sh/uv/)

Copy `.env.example` to `.env` and fill in the values that apply to you.

## Docker

```bash
cp .env.example .env   # fill in your values
docker compose up --build
```

The dashboard is served at `http://localhost:8080`. On subsequent runs `--build` can be omitted. All `TUIDASH_*` settings are read from `.env` at startup.

## Keybindings

| Key | Action |
|---|---|
| `q` | Quit |
| `r` | Refresh all widgets |
| `p` | Toggle privacy mode (masks monetary values) |
| `[` / `]` | Decrease / increase refresh interval by 60 s |
