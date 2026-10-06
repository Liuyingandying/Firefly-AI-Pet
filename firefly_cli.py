"""Firefly developer/agent control CLI. Deliberately no chat command."""
import argparse
import json
from core.control.service import ControlService, ControlError
from core.control.ipc import call


def main(argv=None):
    parser = argparse.ArgumentParser(prog='firefly', description='Firefly system control (JSON output; no chat)')
    commands = parser.add_subparsers(dest='area', required=True)
    commands.add_parser('status'); commands.add_parser('doctor')
    for area, actions in [('model', ('list','status','use')), ('skill', ('list',)),
                           ('learning', ('status',)), ('session', ('list',))]:
        sub = commands.add_parser(area).add_subparsers(dest='action', required=True)
        for action in actions:
            cmd = sub.add_parser(action)
            if action == 'use':
                cmd.add_argument('profile', help='tju-stable / tju-max / auto (model aliases accepted)')
    args = parser.parse_args(argv)
    command = args.area + ('.' + args.action if getattr(args, 'action', None) else '')
    profile = getattr(args, 'profile', None)
    try:
        response = call(command, profile)
        if response is None:
            response = {'ok': True, 'source': 'offline_read_only',
                        'result': ControlService().execute(command, profile)}
        else:
            response['source'] = 'live_runtime'
        print(json.dumps(response, ensure_ascii=False, indent=2))
        return 0 if response['ok'] else 3
    except ControlError as exc:
        print(json.dumps({'ok': False, 'error': exc.code}, ensure_ascii=False))
        return 3
    except Exception:
        print(json.dumps({'ok': False, 'error': 'CONTROL_OPERATION_FAILED'}))
        return 4


if __name__ == '__main__':
    raise SystemExit(main())
