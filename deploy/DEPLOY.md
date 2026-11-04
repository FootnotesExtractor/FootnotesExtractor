# Running FootnotesExtractor on a server

These steps take a fresh Ubuntu or Debian server with sudo access to the app running at
`https://yourdomain.example`, kept running by systemd, with Caddy in front for HTTPS.

## 1. Get the code

```bash
sudo mkdir -p /opt/footnotes
sudo chown $USER:$USER /opt/footnotes
git clone https://github.com/FootnotesExtractor/FootnotesExtractor.git /opt/footnotes/web_app
cd /opt/footnotes/web_app
```

## 2. Install the app and the model weights

```bash
sudo apt update && sudo apt install -y python3-venv python3-pip
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
./venv/bin/python setup_models.py
```

`requirements.txt` installs PyTorch for CUDA 12.4. Check what your NVIDIA driver supports with
`nvidia-smi`, shown as "CUDA Version" at the top right. If it needs a different version, change the
`--extra-index-url` line and the `torch` and `torchvision` versions in `requirements.txt` to a matching
combination from <https://pytorch.org/get-started/locally/> before installing.

A mismatch gives no error, so check that the GPU is found:

```bash
./venv/bin/python -c "import torch; print(torch.cuda.is_available())"
```

`False` on a machine with a GPU almost always means this mismatch, not a missing driver.

## 3. Create a dedicated user

Don't run the app as root:

```bash
sudo useradd --system --home /opt/footnotes/web_app --shell /usr/sbin/nologin footnotes
sudo chown -R footnotes:footnotes /opt/footnotes
```

## 4. Start the app with systemd

If you used other paths or another user, first edit `User=`, `Group=`, `WorkingDirectory=` and
`ExecStart=` in `deploy/footnotes-app.service`.

```bash
sudo cp deploy/footnotes-app.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now footnotes-app
sudo systemctl status footnotes-app       # should show "active (running)"
journalctl -u footnotes-app -f            # follow the logs
```

The unit makes the app listen on `127.0.0.1:5002` only, so Caddy is the only way in from the network.

## 5. Set up HTTPS with Caddy

```bash
# Official install instructions: https://caddyserver.com/docs/install#debian-ubuntu-raspbian
sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt update && sudo apt install -y caddy
```

In `deploy/Caddyfile`, replace `yourdomain.example` with your domain, which must already point at this
server. Then:

```bash
sudo cp deploy/Caddyfile /etc/caddy/Caddyfile
sudo systemctl reload caddy
```

Caddy gets and renews the Let's Encrypt certificate by itself.

No domain yet? Use `deploy/Caddyfile.selfsigned` instead, with your server's IP address in it. Browsers
will show a certificate warning until you switch to a real domain.

## 6. Open the firewall

Only SSH, 80 and 443 need to be reachable from the internet:

```bash
sudo ufw allow OpenSSH
sudo ufw allow 80,443/tcp
sudo ufw enable
```

## 7. Create the first account

```bash
curl -I https://yourdomain.example/login   # expect: HTTP/2 200
```

Open `https://yourdomain.example/register` in a browser. The first account you create becomes the
administrator, and registration then closes. You create every further account in **Admin → Users**.

## Updating

```bash
cd /opt/footnotes/web_app
git pull
./venv/bin/pip install -r requirements.txt
sudo systemctl restart footnotes-app
```

## Backups

Your accounts and work live in `app.db`, `uploads/` and `results/` in the app's folder. `git pull`
leaves them alone, but back them up yourself to keep restore points.
