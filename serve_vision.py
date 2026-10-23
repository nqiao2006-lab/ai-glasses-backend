"""Single-process HTTP server with one persistent OCR child process."""
import argparse
import os
from pathlib import Path
import threading
import uvicorn


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--host',default='127.0.0.1')
    parser.add_argument('--port',type=int,default=8000)
    parser.add_argument('--data-dir',type=Path)
    parser.add_argument('--stop-file',type=Path,help='Test harness: stop gracefully when this file appears')
    args = parser.parse_args()
    if args.host not in {'127.0.0.1','localhost','::1'} and not os.environ.get('VISION_API_TOKEN'):
        parser.error('绑定局域网/公网地址前请设置 VISION_API_TOKEN；HTTPS应由反向代理提供。')
    if args.data_dir:
        os.environ['VISION_DATA_DIR'] = str(args.data_dir.resolve())
    # Exactly one application worker; SQLite + native-model lifecycle assume a single dispatcher.
    config=uvicorn.Config('vision_service.api:create_app',factory=True,host=args.host,port=args.port,workers=1,
                          limit_concurrency=64,timeout_keep_alive=5,access_log=False)
    server=uvicorn.Server(config)
    if args.stop_file:
        def control():
            # A blocking stdin reader can hold a Windows IO lock during subprocess startup.
            while not args.stop_file.exists() and not server.should_exit:
                threading.Event().wait(.2)
            server.should_exit=True
        threading.Thread(target=control,daemon=True).start()
    server.run()


if __name__=='__main__':
    main()
