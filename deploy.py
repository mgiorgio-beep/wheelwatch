"""Deploy Wheelhouse to Beelink server via SSH."""
import paramiko
import os
import secrets
import time
from dotenv import load_dotenv

load_dotenv('.env.deploy')

SSH_HOST = os.environ.get('WH_SSH_HOST', 'ssh.rednun.com')
SSH_PORT = int(os.environ.get('WH_SSH_PORT', '2222'))
SSH_USER = os.environ.get('WH_SSH_USER', 'rednun')
SSH_PASS = os.environ.get('WH_SSH_PASS', '')

LOCAL_DIR = os.path.dirname(os.path.abspath(__file__))

ANTHROPIC_KEY = os.environ.get('ANTHROPIC_API_KEY', '')
WH_PASSWORD = os.environ.get('WHEELHOUSE_PASSWORD', 'wheelhouse')

def ssh_connect():
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(SSH_HOST, port=SSH_PORT, username=SSH_USER, password=SSH_PASS, timeout=15)
    return client

def run(client, cmd, check=True):
    print(f'  $ {cmd[:120]}')
    stdin, stdout, stderr = client.exec_command(cmd, timeout=120)
    out = stdout.read().decode()
    err = stderr.read().decode()
    rc = stdout.channel.recv_exit_status()
    if out.strip():
        for line in out.rstrip().split('\n')[:30]:
            print(f'    {line}')
    if err.strip() and rc != 0:
        for line in err.rstrip().split('\n')[:10]:
            print(f'    [err] {line}')
    if check and rc != 0:
        print(f'    [exit code {rc}]')
    return out, err, rc

def upload_file(sftp, local_path, remote_path):
    print(f'  UPLOAD {os.path.basename(local_path)} -> {remote_path}')
    sftp.put(local_path, remote_path)

def upload_content(sftp, content, remote_path):
    print(f'  WRITE {remote_path}')
    with sftp.open(remote_path, 'w') as f:
        f.write(content)

def main():
    print('=== Wheelhouse Deployment ===\n')

    print('[1] Connecting to Beelink...')
    client = ssh_connect()
    sftp = client.open_sftp()
    print('    Connected!\n')

    print('[2] Checking port 8090...')
    out, _, _ = run(client, 'ss -tlnp 2>/dev/null | grep 8090 || echo "PORT_FREE"', check=False)

    print('\n[3] Creating /opt/wheelhouse...')
    run(client, 'sudo mkdir -p /opt/wheelhouse/static', check=False)
    run(client, 'sudo chown -R rednun:rednun /opt/wheelhouse')

    print('\n[4] Setting up Python venv + dependencies...')
    run(client, 'cd /opt/wheelhouse && python3 -m venv venv 2>&1')
    run(client, '/opt/wheelhouse/venv/bin/pip install --upgrade pip 2>&1 | tail -1', check=False)
    out, _, rc = run(client, '/opt/wheelhouse/venv/bin/pip install flask gunicorn requests python-dotenv 2>&1 | tail -3')
    if rc != 0:
        print('ERROR: pip install failed!')
        return

    print('\n[5] Uploading application files...')
    upload_file(sftp, os.path.join(LOCAL_DIR, 'fishing_intel.py'), '/opt/wheelhouse/fishing_intel.py')
    upload_file(sftp, os.path.join(LOCAL_DIR, 'captain_advisor.py'), '/opt/wheelhouse/captain_advisor.py')
    upload_file(sftp, os.path.join(LOCAL_DIR, 'static', 'fishing.html'), '/opt/wheelhouse/static/fishing.html')
    upload_file(sftp, os.path.join(LOCAL_DIR, 'server.py'), '/opt/wheelhouse/server.py')
    upload_file(sftp, os.path.join(LOCAL_DIR, 'static', 'nco_logo.jpg'), '/opt/wheelhouse/static/nco_logo.jpg')

    print('\n[7] Creating .env...')
    secret_key = secrets.token_hex(32)
    env_content = (
        f'ANTHROPIC_API_KEY={ANTHROPIC_KEY}\n'
        f'WHEELHOUSE_PASSWORD={WH_PASSWORD}\n'
        f'SECRET_KEY={secret_key}\n'
    )
    upload_content(sftp, env_content, '/opt/wheelhouse/.env')
    run(client, 'chmod 600 /opt/wheelhouse/.env')

    print('\n[8] Testing imports...')
    run(client, 'cd /opt/wheelhouse && /opt/wheelhouse/venv/bin/python -c "import fishing_intel; print(\'fishing_intel OK\')"')
    run(client, 'cd /opt/wheelhouse && /opt/wheelhouse/venv/bin/python -c "import captain_advisor; print(\'captain_advisor OK\')"')
    run(client, 'cd /opt/wheelhouse && /opt/wheelhouse/venv/bin/python -c "from server import app; print(\'server OK\')"')

    print('\n[9] Creating systemd service...')
    service_unit = """[Unit]
Description=Wheelhouse Fishing Intel
After=network.target

[Service]
User=rednun
WorkingDirectory=/opt/wheelhouse
Environment=PATH=/opt/wheelhouse/venv/bin:/usr/bin
EnvironmentFile=/opt/wheelhouse/.env
ExecStart=/opt/wheelhouse/venv/bin/gunicorn server:app -b 127.0.0.1:8090 -w 2 --timeout 60
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
"""
    # Write via base64 to avoid quoting issues
    import base64
    b64 = base64.b64encode(service_unit.encode()).decode()
    run(client, f'echo {b64} | base64 -d | sudo tee /etc/systemd/system/wheelhouse.service > /dev/null')

    print('\n[10] Starting service...')
    run(client, 'sudo systemctl daemon-reload')
    run(client, 'sudo systemctl enable wheelhouse 2>&1', check=False)
    run(client, 'sudo systemctl restart wheelhouse')

    time.sleep(3)

    print('\n[11] Service status...')
    run(client, 'sudo systemctl status wheelhouse --no-pager -l 2>&1 | head -15', check=False)

    print('\n[12] Testing local endpoint...')
    run(client, 'curl -s http://127.0.0.1:8090/login | head -5', check=False)

    print('\n[13] Recent logs...')
    run(client, 'sudo journalctl -u wheelhouse --no-pager -n 15 2>&1', check=False)

    sftp.close()
    client.close()

    print('\n' + '='*50)
    print('DEPLOYMENT COMPLETE!')
    print('='*50)
    print()
    print('NEXT STEP — Add Cloudflare tunnel hostname:')
    print('  Cloudflare Zero Trust -> Access -> Tunnels')
    print('  -> Your tunnel -> Public Hostnames -> Add:')
    print('    Subdomain: wheelhouse')
    print('    Domain:    rednun.com')
    print('    Service:   HTTP://localhost:8090')
    print()
    print('Then visit: https://wheelhouse.rednun.com')
    print(f'Password:  {WH_PASSWORD}')

if __name__ == '__main__':
    main()
