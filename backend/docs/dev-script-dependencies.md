# Dev-only dependencies for helper scripts

A few helper scripts under `server/` import packages that the API server never loads. They were never declared, so pyright reported them as unresolved imports. They belong in the `dev` dependency group, not in the production `requirements.txt`.

| Package | Used by | Purpose | Official docs |
|---|---|---|---|
| `pandas` | `server/Utils/excel_to_db.py` | Reads the teacher Excel sheet (`EXCEL_TEACHER_PATH`) into the SQLite DB | https://pandas.pydata.org/docs/ |
| `openpyxl` | the same script, through `pandas.read_excel` | The engine pandas needs to read `.xlsx` files | https://openpyxl.readthedocs.io/ |
| `sounddevice` | `server/Test/imitation_frontend_test.py` | Records microphone audio to exercise `/upload` by hand | https://python-sounddevice.readthedocs.io/ |
| `tabulate` | `server/Test/test_event_management.py` | Prints result tables in the manual event-API script | https://github.com/astanin/python-tabulate |
| `numpy` | `server/Utils/align_connectors.py` (and its test) | Least-squares fit (`numpy.linalg.lstsq`) of one floor map onto another, to name lifts/stairs consistently | https://numpy.org/doc/stable/reference/generated/numpy.linalg.lstsq.html |

## Install

```powershell
# from backend/
uv add --dev pandas openpyxl sounddevice tabulate
uv add --dev numpy
```

## Notes

- `sounddevice` needs the PortAudio library. On Windows the wheel bundles it; on Linux, install `libportaudio2` first.
- `imitation_frontend_test.py` and `test_event_management.py` are manual scripts that call a running server at `BASE_URL`. They are not part of the automated pytest suite.
- pandas ships its own type information, so pyright needs no extra stub package. `tabulate`'s stubs come bundled with pyright.
- numpy (2.2.6 installed) already arrives through spacy and pandas, but `align_connectors.py` imports it directly, so it is declared explicitly. Best practices followed:
  - `rcond=None` is passed to `lstsq`. Since numpy 2.0 that's the default: machine precision times `max(M, N)`.
  - Fit quality is reported, as the median and max residual distance, rather than trusting the fit blindly.
- numpy also ships its own type information.
