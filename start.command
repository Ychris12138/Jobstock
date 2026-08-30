#!/bin/bash
cd "$(dirname "$0")"
( sleep 1; open http://localhost:8770 ) &
python3 server.py
