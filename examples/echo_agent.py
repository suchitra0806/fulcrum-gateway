#!/usr/bin/env python3
"""echo_agent.py — A simple aX agent in Python.

Receives a mention, responds with the message reversed.
Proves that any Python script can be an aX agent.

Usage:
    ax listen --agent my_agent --exec "python examples/echo_agent.py"
"""

import os
import sys

content = sys.argv[-1] if len(sys.argv) > 1 else os.environ.get("AX_MENTION_CONTENT", "")
print(f"You said: {content}\nReversed: {content[::-1]}")
