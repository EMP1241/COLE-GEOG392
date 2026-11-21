# Cost Of Living Explorer

**Temporary GEOG 392 course repository.** Brandon Santor is sharing COLE’s copyrighted source here for course evaluation. He will remove this repository when grading is complete.

© 2026 Brandon Santor for his original COLE work. All rights reserved except as licensed. The limited-use license explains when written permission is required to modify, redistribute or sell the original software. Contributor and third-party rights are documented separately. [COLE license](LICENSE) · [Copyright and permissions](COPYRIGHT.md) · [Third-party notices](THIRD_PARTY_NOTICES.md)

COLE 1.0.1 compares a household's take-home pay with estimated living costs for 3,143 U.S. counties and county equivalents. Choose a household, search the map and compare up to three counties. Optional filters cover temperatures, natural hazards, industry employment, poverty and education, age, and public outdoor areas.

[Open the web application](https://costoflivingexplorer.onrender.com) · [Windows download](https://github.com/EMP1241/cole-downloads/releases/tag/v1.0.1)

## Run the application

Use Python 3.11 or later. The application needs Flask and Waitress. Python supplies SQLite, and the checkout includes Leaflet and the prepared data. The pinned runtime dependencies are in [deploy/requirements-hosted.txt](deploy/requirements-hosted.txt).

On Windows, run these commands in PowerShell from the repository root. Calling the environment's Python directly also works when PowerShell script activation is disabled.

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r deploy/requirements-hosted.txt
.\.venv\Scripts\python.exe -m pip install --no-deps -e .
.\.venv\Scripts\python.exe scripts/hosted_data.py restore
.\.venv\Scripts\python.exe run_cole.py
```

On macOS or Linux, use the same steps with the environment's POSIX path.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r deploy/requirements-hosted.txt
.venv/bin/python -m pip install --no-deps -e .
.venv/bin/python scripts/hosted_data.py restore
.venv/bin/python run_cole.py
```

The restore verifies and unpacks 24 data files from the included archive. The launcher opens the welcome screen in the default browser. After setup, the local app can run offline. Use **Quit application** to stop it.

## Reproduce the report results

In the app, choose two adults and one child. Enter a single annual take-home amount of $85,000. Select Texas and keep estimated costs, with no extra expenses, cost adjustments, or other filters. Find counties once with the money-left goal blank and again with a 20% goal. The first search returns 247 of 254 counties. The second returns 140. To reproduce the age example, retain the goal, set maximum median age to 40 and minimum share aged 18–24 to 15%. Five counties meet all three conditions.

Using the same Python environment as above, the first command below checks the submitted searches against the prepared cost and age tables and prints the results. The second installs the optional test runner, and the third runs the included regression tests. In the commands below, `python` means the environment's Python executable.

```sh
python scripts/reproduce_course_results.py
python -m pip install pytest
python -m pytest -q
```

To reproduce the Texas map, install the plotting packages in that environment and choose an output file.

```sh
python -m pip install "matplotlib>=3.8" "pyproj>=3.6" "shapely>=2.0"
python scripts/plot_course_budget.py texas-budget.png
```

## Source and data

- [Application](src/cole) contains the Python calculations, browser interface, map assets and the five add-on modules. [run_cole.py](run_cole.py) starts the local server.
- [Data preparation](data/README.md) documents publisher files, source years, five-digit county identifiers and the treatment of missing values. The source scripts and the feature preparation code preserve the methods. Rebuilding the research databases from raw inputs is optional and requires the corresponding publisher files and GIS tools. The bundled archive supplies everything needed for normal use and the report example.
- [Data and third-party notices](THIRD_PARTY_NOTICES.md) identify the datasets and dependencies. The 2026 Economic Policy Institute Family Budget Calculator supplies renter budgets in 2025 dollars. COLE subtracts EPI's published annual taxes from its published total before comparing costs with take-home pay. A city or ZIP-area search returns the associated county estimates.

## Team and terms

Brandon Santor created and developed COLE, integrated the add-ons and curated the replacement public outdoor-area data. Bingqian Chen contributed natural hazards, Khayman Petrie industry employment, Andrea Figueroa poverty and education, Mitch Zhang the age profile, and Margaret Worsham the public outdoor-area concept and initial handoff.

The EPI data and COLE’s adaptation of them carry their own license. [CC BY-NC-SA 4.0](licenses/CC-BY-NC-SA-4.0.txt)
