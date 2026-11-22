"""Run the evaluation app; default demonstration needs no Docker or API key.

python examples/47_swebench_evaluation.py demo --output ../.tmp/swe-demo-001
"""

from pi_agent_py.application.evaluation.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
