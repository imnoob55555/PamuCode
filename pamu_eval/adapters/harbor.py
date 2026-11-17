"""Optional Harbor installed-agent bridge. Install Harbor in a separate eval environment.

Task images need the selected Python 3.13 interpreter. A local built wheel is
uploaded explicitly; the task repository's Python environment is never replaced.
"""
import json
from pathlib import Path
import shlex

from harbor.agents.installed.base import BaseInstalledAgent, with_prompt_template
from harbor.agents.agent_auth_resolution import ModelConnectionSpec, ProviderAccess
from harbor.agents.capabilities import AgentCapabilities
from harbor.models.model_api_format import ModelApiFormat


class PamuCodeAgent(BaseInstalledAgent):
    capabilities = AgentCapabilities(base_url=True)
    MODEL_CONNECTION = ModelConnectionSpec(
        default_provider='anthropic',
        agent_provider_access=ProviderAccess(
            api_key_candidate_names=['ANTHROPIC_API_KEY'],
            base_url_candidate_names=['ANTHROPIC_BASE_URL']),
        supported_api_formats=[ModelApiFormat.ANTHROPIC_MESSAGES],
        default_api_format_by_provider={'default': ModelApiFormat.ANTHROPIC_MESSAGES},
        api_key_destination_names={'default': 'ANTHROPIC_API_KEY'},
        base_url_destination_names={'default': 'ANTHROPIC_BASE_URL'})

    def __init__(self, *args, wheel_path, protocol='anthropic_messages',
                 python_bin='python3.13', timeout=300, **kwargs):
        super().__init__(*args, **kwargs)
        self.wheel_path = Path(wheel_path).resolve()
        if not self.wheel_path.is_file() or self.wheel_path.suffix != '.whl':
            raise ValueError('wheel_path must name a locally built PamuCode wheel')
        self.protocol, self.python_bin, self.timeout = protocol, python_bin, int(timeout)
        if protocol != 'anthropic_messages' or self.timeout <= 0:
            raise ValueError('Harbor adapter currently supports Anthropic Messages with a positive timeout')

    @staticmethod
    def name():
        return 'pamucode'

    def version(self):
        return '0.1.0'

    async def install(self, environment):
        remote = '/tmp/' + self.wheel_path.name
        await environment.upload_file(self.wheel_path, remote)
        result = await self.exec_as_root(environment, command=(
            f'{shlex.quote(self.python_bin)} -m venv /opt/pamu-eval && '
            f'/opt/pamu-eval/bin/python -m pip install {shlex.quote(remote)}'))
        if result.return_code != 0:
            raise RuntimeError('PamuCode installation failed; image needs Python 3.13 and dependency access')

    @with_prompt_template
    async def run(self, instruction, environment, context):
        connection = self.model_connection
        if not connection.model_id:
            raise ValueError('Harbor --model is required')
        remote_task = '/tmp/pamu-eval-instruction.txt'
        await self._upload_config_text(environment, content=instruction,
                                       remote_path=remote_task, filename='instruction.txt')
        # Harbor collects /logs/agent after the run. Native files remain useful
        # even when the optional ATIF conversion cannot represent an interrupted call.
        command = shlex.join(['/opt/pamu-eval/bin/pamu', 'run', '--task-file', remote_task,
                              '--output', '/logs/agent/pamu', '--model', connection.model_id,
                              '--protocol', self.protocol, '--timeout', str(self.timeout)])
        result = await self.exec_as_agent(environment, command=command,
                                           env={**connection.env, 'PAMU_PROVIDER': 'anthropic'},
                                           timeout_sec=self.timeout + 5)
        if result.return_code != 0:
            raise RuntimeError('PamuCode did not complete; inspect pamu/result.json and native events')

    def populate_context_post_run(self, context):
        from .trajectory import to_atif
        native = self.logs_dir / 'pamu'
        result_path = native / 'result.json'
        events_path = native / 'events.jsonl'
        if not result_path.exists() or not events_path.exists():
            return
        result = json.loads(result_path.read_text())
        events = [json.loads(line) for line in events_path.read_text().splitlines() if line.strip()]
        try:
            trajectory = to_atif(events, result, model=self.model_connection.model_id)
            from harbor.models.trajectories import Trajectory
            validated = Trajectory.model_validate(trajectory)
        except ValueError:
            return
        (self.logs_dir / 'trajectory.json').write_text(json.dumps(validated.to_json_dict(), indent=2))
        # ATIF capability is intentionally not advertised until real Harbor validation
        # is included in the caller's integration gate. Native artifacts always persist.
