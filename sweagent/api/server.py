try:
    import flask  # noqa
except ImportError as e:
    msg = (
        "Flask not found. You probably haven't installed the dependencies for SWE-agent. "
        "Please go to the root of the repository and run `pip install -e .`"
    )
    raise RuntimeError(msg) from e
from contextlib import redirect_stderr, redirect_stdout
import json
import os
from pathlib import Path
import time
import traceback
from typing import Any, Dict
from flask import Flask, render_template, request, make_response
import sys

import yaml

from sweagent import CONFIG_DIR, PACKAGE_DIR
from sweagent.agent.agents import AgentArguments
from sweagent.agent.models import ModelArguments
from sweagent.api.utils import ThreadWithExc
from sweagent.environment.swe_env import EnvironmentArguments
from sweagent.api.hooks import EnvUpdateHook, WebUpdate, MainUpdateHook, AgentUpdateHook
import sweagent.environment.utils as env_utils
from flask_socketio import SocketIO
from flask_cors import CORS
from flask import session
from uuid import uuid4
import tempfile
import atexit

# baaaaaaad
sys.path.append(str(PACKAGE_DIR.parent))
from run import ActionsArguments, ScriptArguments, Main

app = Flask(__name__)
CORS(app)
socketio = SocketIO(app, cors_allowed_origins="*")
# Setting these variables outside of `if __name__ == "__main__"` because when run Flask server with
# `flask run` it will skip the if block. Therefore, the app will return an error for missing `secret_key`
# Setting it here will allow both `flask run` and `python server.py` to work
app.secret_key = "super secret key"
app.config["SESSION_TYPE"] = "memcache"

THREADS: Dict[str, "MainThread"] = {}

os.environ["SWE_AGENT_EXPERIMENTAL_COMMUNICATE"] = "1"
env_utils.START_UP_DELAY = 1


def ensure_session_id_set():
    """Ensures a session ID is set for this user"""
    session_id = session.get("session_id", None)
    if not session_id:
        session_id = uuid4().hex
        session["session_id"] = session_id
    return session_id


class MainThread(ThreadWithExc):
    def __init__(self, settings: ScriptArguments, wu: WebUpdate):
        super().__init__()
        self._wu = wu
        self._settings = settings

    def run(self) -> None:
        # fixme: This actually redirects all output from all threads to the socketio, which is not what we want
        with redirect_stdout(self._wu.log_stream):
            with redirect_stderr(self._wu.log_stream):
                try:
                    main = Main(self._settings)
                    main.add_hook(MainUpdateHook(self._wu))
                    main.agent.add_hook(AgentUpdateHook(self._wu))
                    main.env.add_hook(EnvUpdateHook(self._wu))
                    main.main()
                except Exception as e:
                    self._wu.up_agent(f"Error (see log for details): {e}")
                    traceback_str = traceback.format_exc()
                    self._wu.up_log(traceback_str)
                    self._wu.up_log(str(e), level="critical")
                    self._wu.finish_run()
                    raise

    def stop(self):
        while self.is_alive():
            self.raise_exc(SystemExit)
            time.sleep(0.1)
        self._wu.finish_run()
        self._wu.up_agent("Run stopped by user")


@app.route("/")
def index():
    return render_template("index.html")


@socketio.on("connect")
def handle_connect():
    print("Client connected")


def write_env_yaml(data: Dict[str, Any]) -> str:
    path = Path(tempfile.NamedTemporaryFile(delete=False, suffix=".yml").name)
    atexit.register(path.unlink)
    path.write_text(yaml.dump(data))
    return str(path)


@app.route("/run", methods=["GET", "OPTIONS"])
def run():
    session_id = ensure_session_id_set()
    if request.method == "OPTIONS":  # CORS preflight
        return _build_cors_preflight_response()
    # While we're running as a local UI, let's make sure that there's at most
    # one run at a time
    global THREADS
    for thread in THREADS.values():
        if thread.is_alive():
            thread.stop()
    wu = WebUpdate(socketio)
    wu.up_agent("Starting the run")
    data_path = request.args["data_path"]
    repo_path = request.args["repo_path"]
    model_name = request.args["model"]
    environment = json.loads(request.args["environment"])
    environment_setup = ""
    if environment["config_type"] == "manual":
        if not environment["install_command_active"]:
            environment["install"] = ""
        environment_setup = str(write_env_yaml(environment))
    elif environment["config_type"] == "script_path":
        environment_setup = environment["script_path"]
    if not environment_setup.strip():
        environment_setup = None
    test_run = request.args["test_run"].lower() == "true"
    if test_run:
        model_name = "instant_empty_submit"
    defaults = ScriptArguments(
        suffix="",
        environment=EnvironmentArguments(
            image_name="sweagent/swe-agent:latest",
            data_path=data_path,
            split="dev",
            verbose=True,
            install_environment=True,
            repo_path=repo_path,
            environment_setup=environment_setup,
        ),
        skip_existing=False,
        agent=AgentArguments(
            model=ModelArguments(
                model_name=model_name,
                total_cost_limit=0.0,
                per_instance_cost_limit=3.0,
                temperature=0.0,
                top_p=0.95,
            ),
            config_file=CONFIG_DIR / "default_from_url.yaml",
        ),
        actions=ActionsArguments(open_pr=False, skip_if_commits_reference_issue=True),
        raise_exceptions=True,
    )
    thread = MainThread(defaults, wu)
    THREADS[session_id] = thread
    thread.start()
    return "Commands are being executed", 202


@app.route("/stop")
def stop():
    session_id = ensure_session_id_set()
    global THREADS
    print(f"Stopping session {session_id}")
    print(THREADS)
    thread = THREADS.get(session_id)
    if thread and thread.is_alive():
        print(f"Thread {thread} is alive")
        thread.stop()
    else:
        print(f"Thread {thread} is not alive")
    return "Stopping computation", 202


def _build_cors_preflight_response():
    response = make_response()
    response.headers.add("Access-Control-Allow-Origin", "*")
    response.headers.add("Access-Control-Allow-Headers", "*")
    response.headers.add("Access-Control-Allow-Methods", "*")
    return response


if __name__ == "__main__":
    app.debug = True
    socketio.run(app, port=8000, debug=True)
