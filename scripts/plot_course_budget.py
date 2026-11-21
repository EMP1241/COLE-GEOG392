"""Draw the report's Texas county budget map from the prepared release."""

import argparse
import csv
import json
from collections import Counter
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle
from pyproj import Transformer
from shapely.geometry import shape
from shapely.ops import transform, unary_union
from shapely.plotting import plot_polygon

DATA = Path(__file__).resolve().parents[1] / "data/prepared"
# These breaks use the household's pay and reserve, so they have the same
# meaning in every county. The hatching also identifies a deficit in grayscale.
FILLS = ("#357777", "#EAD6AA", "#6C3C68")
HATCHES = (None, None, "////")
LABEL_OFFSETS = {"48215": (-31, 2), "48041": (18, 8), "48453": (-22, -15)}


def read_counties():
    with (DATA / "households.csv").open(encoding="utf-8", newline="") as source:
        costs = {
            row["county_fips"]: int(row["published_annual_total"])
            - int(row["published_annual_taxes"])
            for row in csv.DictReader(source)
            if row["state_abbr"] == "TX" and row["household_profile"] == "2p1c"
        }
    with (DATA / "counties.geojson").open(encoding="utf-8") as source:
        features = json.load(source)["features"]
    project = Transformer.from_crs("EPSG:4326", "EPSG:3083", always_xy=True).transform
    counties = {}
    for feature in features:
        props = feature["properties"]
        if props["state_abbr"] == "TX":
            fips = props["county_fips"]
            cost = costs[fips]
            band = 0 if cost <= 68_000 else 1 if cost <= 85_000 else 2
            counties[fips] = (
                transform(project, shape(feature["geometry"])),
                band,
                props["county_name"],
            )
    if len(costs) != 254 or counties.keys() != costs.keys():
        raise ValueError("The cost and boundary tables must cover the same 254 Texas counties.")
    return counties


def draw(output):
    counties = read_counties()
    counts = Counter(band for _, band, _ in counties.values())
    plt.rcParams.update(
        {"font.family": "DejaVu Sans", "hatch.linewidth": 0.4, "text.parse_math": False}
    )
    fig = plt.figure(figsize=(6.95, 3.63), facecolor="white")
    ax = fig.add_axes((0.006, 0.105, 0.595, 0.885))
    ax.set_aspect("equal")
    ax.set_axis_off()
    for geometry, band, _ in counties.values():
        plot_polygon(
            geometry,
            ax=ax,
            add_points=False,
            facecolor=FILLS[band],
            edgecolor="white",
            linewidth=0.34,
            hatch=HATCHES[band],
        )
    outline = unary_union([geometry for geometry, _, _ in counties.values()])
    plot_polygon(outline, ax=ax, add_points=False, fill=False, edgecolor="#4B5755", linewidth=0.6)
    x0, y0, x1, y1 = outline.bounds
    ax.set_xlim(x0 - 46_000, x1 + 42_000)
    ax.set_ylim(y0 - 28_000, y1 + 22_000)

    for fips, offset in LABEL_OFFSETS.items():
        geometry, _, name = counties[fips]
        point = geometry.representative_point()
        ax.plot(point.x, point.y, "o", ms=3.1, color="#172222", mec="white", mew=0.5, zorder=5)
        ax.annotate(
            name.removesuffix(" County"),
            (point.x, point.y),
            xytext=offset,
            textcoords="offset points",
            ha="left" if offset[0] > 0 else "right",
            va="center",
            fontsize=7.7,
            color="#132224",
            zorder=6,
            bbox={"boxstyle": "round,pad=0.15", "fc": "white", "ec": "none", "alpha": 0.96},
            arrowprops={"arrowstyle": "-", "color": "#192E2E", "lw": 0.65, "shrinkB": 2},
        )

    left, bottom = x0 + 45_000, y0 + 110_000
    length = 160_934.4  # 100 statute miles, in the projected CRS's metres.
    ax.plot([left, left + length], [bottom, bottom], color="#34413F", lw=1.05)
    for x in (left, left + length):
        ax.plot([x, x], [bottom - 6_000, bottom + 6_000], color="#34413F", lw=0.85)
    ax.annotate(
        "100 mi",
        (left + length / 2, bottom),
        xytext=(0, 3.5),
        textcoords="offset points",
        ha="center",
        fontsize=7.3,
        color="#34413F",
    )

    fig.text(
        0.608, 0.87, "Annual household costs", fontsize=10.4, fontweight="bold", color="#162725"
    )
    legend = [
        ("Keeps the 20% reserve", "Costs of $68,000 or less"),
        ("Covers costs, below goal", "Over $68,000 to $85,000"),
        ("Costs exceed take-home pay", "Over $85,000"),
    ]
    for band, (heading, cutoff) in enumerate(legend):
        y = 0.695 - 0.21 * band
        key = fig.add_axes((0.608, y + 0.004, 0.029, 0.04))
        key.add_patch(
            Rectangle(
                (0, 0),
                1,
                1,
                facecolor=FILLS[band],
                hatch=HATCHES[band],
                edgecolor="white",
                linewidth=0.35,
            )
        )
        key.set_axis_off()
        fig.text(0.651, y + 0.018, heading, fontsize=8.9, fontweight="bold", color="#1F2826")
        fig.text(0.651, y - 0.052, cutoff, fontsize=8.1, color="#293431")
        fig.text(0.651, y - 0.112, f"{counts[band]} counties", fontsize=8.1, color="#293431")
    fig.text(
        0.02,
        0.031,
        "EPI 2026 and U.S. Census Bureau 2021   ·   Texas Centric Albers Equal Area (EPSG:3083)",
        fontsize=7.1,
        color="#424947",
    )
    fig.savefig(output, dpi=320, facecolor="white")
    plt.close(fig)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, help="Destination PNG or PDF file")
    draw(parser.parse_args().output)
