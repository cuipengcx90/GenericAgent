#!/bin/zsh
cd /Users/cuip/projects/GenericAgent || exit 1
exec .venv/bin/python frontends/tuiapp_v2.py "$@"
