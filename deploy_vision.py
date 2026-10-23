"""Render/local entry point. No .env from the legacy B service is imported."""
import argparse
import os
import uvicorn


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--host', default='0.0.0.0')
    parser.add_argument('--port', type=int, default=os.environ.get('PORT', '8001'))
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error('port must be between 1 and 65535')
    uvicorn.run('deployment:create_app', factory=True, host=args.host, port=args.port,
                workers=1, limit_concurrency=64, timeout_keep_alive=5, access_log=False)


if __name__ == '__main__':
    main()
