set dotenv-load

# list recipes
default:
    @just --list

# install / update dependencies
sync:
    uv sync

# log a sender account in: just login alice [user-id]
login account user="":
    uv run python app.py --login {{account}}{{ if user != "" { " --user " + user } else { "" } }}

# start the control bot
run:
    uv run python app.py

# offline logic checks
check:
    uv run python app.py --self-check

# show what would fire right now
dry:
    uv run python app.py --dry-run

# build the container image locally
image:
    docker build -t tg-broadcaster:local .
