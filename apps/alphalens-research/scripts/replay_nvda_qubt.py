"""NVDA→QUBT partial replay (Phase A+B+C).

Feeds the 2026-04-14 NVIDIA Ising press release through Layer 2 (Flash event
extraction) and Layer 3 (Pro theme mapper + 4 verification gates). Tests:

  G1. Layer 2 returns themes including `quantum_computing` (or equivalent).
  G2. Layer 3 mapper surfaces QUBT for the `quantum_computing` theme even
      though the press release does not name it (second-order test).
  G3. As positive control, Layer 3 surfaces IONQ (named in press).
  G4. Verification gates fire on QUBT (any of 4 ⇒ verified).

All intermediate artifacts are written under
``~/.alphalens/replay/nvda_qubt/``.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from alphalens_pipeline.data.alt_data.polygon_client import PolygonClient
from alphalens_pipeline.thematic.extraction import event_extractor
from alphalens_pipeline.thematic.mapping import orchestrator, theme_mapper

REPLAY_DIR = Path.home() / ".alphalens" / "replay" / "nvda_qubt"
REPLAY_DIR.mkdir(parents=True, exist_ok=True)

ASOF = dt.date(2026, 4, 14)
TARGET_SECOND_ORDER = "QUBT"
POSITIVE_CONTROL = "IONQ"

HEADLINE = (
    "NVIDIA Launches Ising, the World's First Open AI Models to Accelerate "
    "the Path to Useful Quantum Computers"
)
BODY = """NVIDIA today announced the world's first family of open source quantum AI models, NVIDIA Ising, designed to help researchers and enterprises build quantum processors capable of running useful applications.

To achieve useful quantum applications at scale, significant breakthroughs are needed in quantum processor calibration and quantum error correction. AI is key for turning today's quantum processors into large-scale, reliable computers. Open models empower developers to build high-performance AI while maintaining total control over their data and infrastructure.

Named after a landmark mathematical model that dramatically simplified the understanding of complex physical systems, the NVIDIA Ising family provides high-performance, scalable AI tools for quantum error correction and calibration — two of the most critical challenges in building hybrid-quantum classical systems.

Ising models run the world's best quantum processor calibration and enable researchers to tackle much larger, more complex problems with quantum computers by delivering up to 2.5x faster performance and 3x higher accuracy for the decoding process needed for quantum error correction.

"AI is essential to making quantum computing practical," said Jensen Huang, founder and CEO of NVIDIA. "With Ising, AI becomes the control plane — the operating system of quantum machines — transforming fragile qubits to scalable and reliable quantum-GPU systems."

The quantum computing market is expected to surpass $11 billion in 2030, according to analyst firm Resonance. This growth trajectory is highly dependent on continued progress in addressing critical engineering challenges, such as quantum error correction and scalability.

NVIDIA Ising includes state-of-the-art customizable models, tools and data that accelerate quantum processors:

Ising Calibration: A vision language model that can rapidly interpret and react to measurements from quantum processors. This enables AI agents to automate continuous calibration, reducing the time needed from days to hours.

Ising Decoding: Two variants of a 3D convolutional neural network model — optimized for either speed or accuracy — to perform real-time decoding for quantum error correction. Ising Decoding models are up to 2.5x faster and 3x more accurate than pyMatching, the current open source industry standard.

Leading enterprises, academic institutions and research labs are adopting Ising for quantum computing development.

Ising Calibration is already in use by Atom Computing, Academia Sinica, EeroQ, Conductor Quantum, Fermi National Accelerator Laboratory, Harvard, Infleqtion, IonQ, IQM Quantum Computers, Lawrence Berkeley National Laboratory, Q-CTRL and the U.K. National Physical Laboratory.

Ising Decoding is being deployed by Cornell University, EdenCode, Infleqtion, IQM Quantum Computers, Quantum Elements, Sandia National Laboratories, SEEQC, University of California San Diego, UC Santa Barbara, University of Chicago, University of Southern California and Yonsei University.

In addition, NVIDIA is providing a cookbook of quantum computing workflows and training data along with NVIDIA NIM microservices, equipping developers to fine-tune models for specific hardware architectures and use cases with minimal setup.

NVIDIA Ising complements the NVIDIA CUDA-Q software platform for hybrid quantum-classical computing and integrates with the NVIDIA NVQLink QPU-GPU hardware interconnect for real-time control and quantum error correction.

NVIDIA Ising joins NVIDIA's open model portfolio, which includes NVIDIA Nemotron for agentic systems, NVIDIA Cosmos for physical AI, NVIDIA Alpamayo for autonomous vehicles, NVIDIA Isaac GR00T for robotics and NVIDIA BioNeMo for biomedical research.

These open models, data and frameworks are available on GitHub, Hugging Face and build.nvidia.com."""

NEWS_ROW = {
    "id": "nvda_ising_2026_04_14",
    "source": "nvidianews.nvidia.com",
    "tickers": ["NVDA"],
    "title": HEADLINE,
    "body": BODY,
    "published_at": pd.Timestamp("2026-04-14T13:00:00Z"),
}


def banner(msg: str) -> None:
    bar = "=" * 78
    print(f"\n{bar}\n{msg}\n{bar}", flush=True)


def step_layer2() -> dict:
    banner("Layer 2 — DeepSeek v4 Flash event extraction")
    api_key = os.environ["OPENROUTER_API_KEY"]
    event = event_extractor.extract_one(NEWS_ROW, api_key=api_key)
    if event is None:
        raise RuntimeError("Layer 2 returned None")
    (REPLAY_DIR / "layer2_event.json").write_text(json.dumps(event, indent=2, default=str))
    print(json.dumps(event, indent=2, default=str))
    return event


def step_layer3_propose(themes: list[str]) -> dict[str, list[dict]]:
    banner(f"Layer 3 — DeepSeek v4 Pro mapper, themes={themes}")
    api_key = os.environ["OPENROUTER_API_KEY"]
    all_by_theme: dict[str, list[dict]] = {}
    for theme in themes:
        cands = theme_mapper.propose_candidates(theme=theme, api_key=api_key)["candidates"]
        all_by_theme[theme] = cands
        print(f"\n--- theme={theme!r}: {len(cands)} candidates ---")
        for c in cands:
            print(
                f"  {c.get('ticker', '?'):6s}  "
                f"conf={c.get('confidence', 0.0):.2f}  "
                f"{c.get('company_name', '')}"
            )
    (REPLAY_DIR / "layer3_candidates.json").write_text(
        json.dumps(all_by_theme, indent=2, default=str)
    )
    return all_by_theme


def step_gates(ticker: str, themes: list[str]) -> dict:
    banner(f"Layer 3 verification gates — ticker={ticker}, themes={themes}")
    polygon_key = os.environ.get("POLYGON_API_KEY", "")
    polygon_client = PolygonClient(api_key=polygon_key) if polygon_key else None
    verdict = orchestrator.verify_candidate(
        ticker=ticker,
        themes=themes,
        asof=ASOF,
        polygon_client=polygon_client,
    )
    print(json.dumps(verdict, indent=2, default=str))
    return verdict


def summarize(event: dict, by_theme: dict, qubt_verdict: dict, ionq_verdict: dict) -> None:
    banner("SUMMARY")
    themes = event.get("themes") or []
    g1 = any(("quantum" in t.lower()) for t in themes)
    qubt_in = {
        t: any(c.get("ticker", "").upper() == TARGET_SECOND_ORDER for c in cs)
        for t, cs in by_theme.items()
    }
    ionq_in = {
        t: any(c.get("ticker", "").upper() == POSITIVE_CONTROL for c in cs)
        for t, cs in by_theme.items()
    }
    g2 = any(qubt_in.values())
    g3 = any(ionq_in.values())
    g4 = qubt_verdict.get("verified", False)

    summary = {
        "asof": ASOF.isoformat(),
        "G1_layer2_quantum_theme": g1,
        "G1_themes_returned": themes,
        "G2_QUBT_surfaced_layer3": g2,
        "G2_QUBT_in_by_theme": qubt_in,
        "G3_IONQ_surfaced_layer3_positive_control": g3,
        "G3_IONQ_in_by_theme": ionq_in,
        "G4_QUBT_gates_passed": qubt_verdict.get("gates_passed", []),
        "G4_QUBT_gates_failed": qubt_verdict.get("gates_failed", []),
        "G4_QUBT_gates_unknown": qubt_verdict.get("gates_unknown", []),
        "G4_QUBT_verified": g4,
        "IONQ_gates_passed": ionq_verdict.get("gates_passed", []),
        "IONQ_gates_failed": ionq_verdict.get("gates_failed", []),
        "IONQ_gates_unknown": ionq_verdict.get("gates_unknown", []),
        "IONQ_verified": ionq_verdict.get("verified", False),
    }
    (REPLAY_DIR / "summary.json").write_text(json.dumps(summary, indent=2, default=str))
    print(json.dumps(summary, indent=2, default=str))

    print()
    for tag, val in (
        ("G1 Layer 2 returned a quantum theme", g1),
        (f"G2 Layer 3 surfaced {TARGET_SECOND_ORDER}", g2),
        (f"G3 Layer 3 surfaced {POSITIVE_CONTROL} (positive control)", g3),
        (f"G4 {TARGET_SECOND_ORDER} verified by ≥1 gate", g4),
    ):
        mark = "PASS" if val else "FAIL"
        print(f"  [{mark}] {tag}")


def main() -> int:
    (REPLAY_DIR / "source.json").write_text(json.dumps(NEWS_ROW, indent=2, default=str))

    event = step_layer2()
    themes = event.get("themes") or []
    # Always include the canonical "quantum_computing" probe so Layer 3 runs
    # even if Layer 2 returns a wildly different label.
    probe_themes = list({*(t for t in themes), "quantum_computing"})
    by_theme = step_layer3_propose(probe_themes)

    qubt_verdict = step_gates(TARGET_SECOND_ORDER, ["quantum_computing"])
    ionq_verdict = step_gates(POSITIVE_CONTROL, ["quantum_computing"])

    summarize(event, by_theme, qubt_verdict, ionq_verdict)
    print(f"\nArtifacts under: {REPLAY_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
