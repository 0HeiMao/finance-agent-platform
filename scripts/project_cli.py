"""Shared implementation for the PowerShell and Bash project commands."""
import argparse
import os
import secrets
import shutil
import socket
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(*args):
    subprocess.run([str(arg) for arg in args], cwd=ROOT, check=True)


def require(name):
    path = shutil.which(name)
    if not path:
        raise RuntimeError(f"Install {name} and add it to PATH first.")
    return path


def python_path():
    path = ROOT / '.venv' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    if not path.exists():
        raise RuntimeError('Run scripts/setup first to create .venv.')
    return path


def check_ports(ports):
    for port in ports:
        with socket.socket() as sock:
            try:
                sock.bind(('127.0.0.1', port))
            except OSError as exc:
                raise RuntimeError(f'Port {port} is occupied; no services were started.') from exc


def compose(*args, demo=False):
    command = [require('docker'), 'compose']
    if demo:
        command += ['--env-file', '.env.demo', '-f', 'docker-compose.demo.yml']
    run(*command, *args)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['setup', 'start', 'check', 'demo'])
    parser.add_argument('--quick', action='store_true', help='check: skip database/frontend tests')
    parser.add_argument('--with-pages', action='store_true', help='check: requires running local UI/API')
    parser.add_argument('--stop', action='store_true', help='start/demo: stop containers; preserve volumes')
    args = parser.parse_args()
    if (args.quick or args.with_pages) and args.command != 'check':
        parser.error('--quick/--with-pages only apply to check')
    if args.stop and args.command not in ('start', 'demo'):
        parser.error('--stop only applies to start/demo')
    if args.command == 'setup':
        if sys.version_info < (3, 11):  # noqa: UP036 -- bootstrap can run before dependencies are installed
            raise RuntimeError('Python 3.11 or newer is required.')
        npm = require('npm.cmd' if os.name == 'nt' else 'npm')
        run(require('node'), '--version')
        env = ROOT / '.env'
        if not env.exists():
            with env.open('x', encoding='utf-8') as stream:
                stream.write((ROOT / '.env.example').read_text(encoding='utf-8').replace(
                    'replace-with-a-long-random-secret', secrets.token_urlsafe(48)))
        if not (ROOT / '.venv').exists():
            run(sys.executable, '-m', 'venv', ROOT / '.venv')
        run(python_path(), '-m', 'pip', 'install', '-r', 'requirements.txt')
        run(npm, '--prefix', 'frontend', 'ci')
        print('Ready. Existing .env preserved. Configure model keys and install the 384-dimensional embedding model separately.')
    elif args.command == 'check':
        flags = (['--quick'] if args.quick else []) + (['--with-pages'] if args.with_pages else [])
        run(python_path(), 'scripts/check_all.py', *flags)
    elif args.command == 'start':
        if not (ROOT / '.env').exists():
            raise RuntimeError('Run setup and configure .env first.')
        if args.stop:
            compose('stop')
        else:
            # Validate without printing resolved secrets.
            compose('config', '--quiet')
            compose('up', '-d', '--build', '--wait', '--wait-timeout', '180')
            print('UI: http://127.0.0.1:8080 | API: http://127.0.0.1:8000/docs')
    else:
        env = ROOT / '.env.demo'
        if not env.exists():
            if args.stop:
                raise RuntimeError('No demo environment has been prepared.')
            with env.open('x', encoding='utf-8') as stream:
                stream.write('DEMO_DB_PASSWORD=' + secrets.token_urlsafe(24) + '\n'
                             'AUTH_SECRET_KEY=' + secrets.token_urlsafe(48) + '\n'
                             'DEMO_ADMIN_PASSWORD=' + secrets.token_urlsafe(24) + '\n')
        if args.stop:
            compose('stop', demo=True)
            return
        compose('config', '--quiet', demo=True)
        # Only check the port on first start; Docker validates conflicts on subsequent starts.
        status = subprocess.check_output([require('docker'), 'compose', '--env-file', '.env.demo',
                                          '-f', 'docker-compose.demo.yml', 'ps', '-q', 'frontend'], cwd=ROOT)
        if not status.strip():
            check_ports([8081])
        compose('up', '-d', '--build', '--wait', '--wait-timeout', '180', demo=True)
        compose('exec', '-T', 'backend', 'python', 'scripts/seed_interview_demo.py', demo=True)
        print('Demo: http://127.0.0.1:8081 | User: interview_admin')
        print('Password: DEMO_ADMIN_PASSWORD in .env.demo. No external model is configured or called.')
        print('Guide: docs/interview-demo-guide.md. Stop with scripts/demo --stop; data is retained.')


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, OSError, subprocess.CalledProcessError) as error:
        print(f'ERROR: {error}', file=sys.stderr)
        sys.exit(1)
