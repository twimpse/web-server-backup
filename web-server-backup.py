#!/usr/bin/python3

import os
import re
import json
import sys
import subprocess
import tempfile
import shutil
import datetime
import hashlib
import random
import string
import time
import smtplib

from ftplib import FTP
from pathlib import Path
from email.message import EmailMessage


CONFIG_FILE = "/root/.webserver-backup-config.json"
APACHE_SITES = "/etc/apache2/sites-enabled"


def load_config():
    with open(CONFIG_FILE, "r") as f:
        return json.load(f)


def random_string(length=10):
    return ''.join(random.choice(string.ascii_letters + string.digits) for _ in range(length))


def sha256sum(filename):
    h = hashlib.sha256()
    with open(filename, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def print_separator():
    print()
    print("------------")
    print()


def get_apache_sites():
    sites = []
    seen = set()

    for file in os.listdir(APACHE_SITES):
        path = os.path.join(APACHE_SITES, file)

        if not os.path.isfile(path):
            continue

        with open(path, "r", errors="ignore") as f:
            content = f.read()

        document_roots = re.findall(
            r"^\s*DocumentRoot\s+(.+)",
            content,
            re.MULTILINE
        )

        for root in document_roots:
            root = root.strip().strip('"')
            real = os.path.realpath(root)

            if real in seen:
                continue

            if not os.path.exists(real):
                continue

            seen.add(real)

            sites.append({
                "name": os.path.basename(real),
                "path": real
            })

    return sites


def detect_wordpress(site):
    wp = os.path.join(site["path"], "wp-config.php")

    if os.path.exists(wp):
        site["type"] = "wordpress"
        site["wp_config"] = wp
    else:
        site["type"] = "static"

    return site


def parse_wp_config(site):
    if site["type"] != "wordpress":
        return site

    with open(site["wp_config"], "r", errors="ignore") as f:
        content = f.read()

    def extract(name):
        m = re.search(
            rf"define\(\s*['\"]{name}['\"]\s*,\s*['\"](.+?)['\"]",
            content
        )
        return m.group(1) if m else None

    site["db"] = {
        "name": extract("DB_NAME")
    }

    return site


def discover_sites():
    sites = get_apache_sites()

    result = []
    for site in sites:
        site = detect_wordpress(site)
        site = parse_wp_config(site)
        result.append(site)

    return result


def dump_database(site, tmpdir):
    if site["type"] != "wordpress":
        return None

    sql = os.path.join(tmpdir, f"{site['name']}.sql")

    cmd = [
        "mysqldump",
        "-u",
        "root",
        site["db"]["name"]
    ]

    with open(sql, "w") as f:
        subprocess.run(cmd, stdout=f, check=True)

    return sql


def create_tar(site, tmpdir, sql):
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d_%H%M%S")

    tar = os.path.join(
        tmpdir,
        f"{site['name']}-{timestamp}.tar"
    )

    subprocess.run(
        ["tar", "-cf", tar, "-C", site["path"], "."],
        check=True
    )

    if sql:
        subprocess.run(
            ["tar", "-rf", tar, "-C", tmpdir, os.path.basename(sql)],
            check=True
        )

    return tar


def compress_archive(tar, config):

    compression = config["compression"]
    program = compression.get("program", "7z")
    level = compression.get("level", 9)

    output = tar + f".{program}"

    if program == "7z":
        cmd = [
            "7z",
            "a",
            f"-mx={level}",
            output,
            tar
        ]
        subprocess.run(cmd, check=True)

    elif program == "gzip":
        subprocess.run(["gzip", "-9", tar], check=True)
        output = tar + ".gz"

    elif program == "xz":
        subprocess.run(["xz", "-9", tar], check=True)
        output = tar + ".xz"

    else:
        raise Exception("Unsupported compression")

    return output


def move_archive(archive, config):

    backup_dir = Path(config["backup_dir"])
    backup_dir.mkdir(parents=True, exist_ok=True)

    dest = backup_dir / Path(archive).name

    shutil.move(archive, dest)

    return dest


def previous_hash(site, config):

    backup_dir = Path(config["backup_dir"])

    files = sorted(
        backup_dir.glob(f"{site}-*"),
        reverse=True
    )

    if len(files) < 2:
        return None

    return sha256sum(files[1])


def ftp_upload(file, config):

    ftp_conf = config["ftp"]

    ftp = FTP(ftp_conf["host"])
    ftp.login(
        ftp_conf["user"],
        ftp_conf["password"]
    )

    ftp.cwd(ftp_conf["remote_dir"])

    temp_name = f"{random_string(12)}.tmp"
    final_name = Path(file).name

    size = os.path.getsize(file)

    print("Uploading...")

    start = time.time()

    with open(file, "rb") as f:
        ftp.storbinary(f"STOR {temp_name}", f)

    ftp.rename(temp_name, final_name)

    end = time.time()

    ftp.quit()

    duration = end - start

    if duration == 0:
        duration = 0.01

    speed = size / duration / 1024 / 1024

    print(f"Upload speed: {speed:.2f} MB/s")

    return speed


def cleanup_versions(site, config):

    backup_dir = Path(config["backup_dir"])
    keep = config.get("keep_versions", 3)

    files = sorted(
        backup_dir.glob(f"{site}-*"),
        reverse=True
    )

    for f in files[keep:]:
        f.unlink()


def latest_backup(site, config):

    backup_dir = Path(config["backup_dir"])

    files = sorted(
        backup_dir.glob(f"{site}-*"),
        reverse=True
    )

    if not files:
        return None

    return files[0]


def backup_site(site, config):

    print(f"Backing up {site['name']}")

    with tempfile.TemporaryDirectory() as tmpdir:

        sql = dump_database(site, tmpdir)

        tar = create_tar(site, tmpdir, sql)

        archive = compress_archive(tar, config)

        new_hash = sha256sum(archive)

        latest = latest_backup(site["name"], config)

        changed = True

        if latest:
            old_hash = sha256sum(latest)
            changed = new_hash != old_hash

        if not changed:
            print("No change")
            print_separator()
            return False, None

        final = move_archive(archive, config)

    print("Everything is OK")

    speed = ftp_upload(final, config)

    cleanup_versions(site["name"], config)

    print_separator()

    return True, speed

def send_email(config, summary):

    email_conf = config.get("email", {})

    if not email_conf.get("enabled", False):
        return

    msg = EmailMessage()

    msg["Subject"] = "Webserver Backup Report"
    msg["From"] = email_conf["from"]
    msg["To"] = email_conf["to"]

    msg.set_content(summary)

    with smtplib.SMTP(email_conf.get("smtp", "localhost")) as s:
        s.send_message(msg)


def main():

    start_time = time.time()

    config = load_config()

    sites = discover_sites()

    changed_sites = []
    errors = []
    upload_speeds = []

    for site in sites:

        try:

            changed, speed = backup_site(site, config)

            if changed:
                changed_sites.append(site["name"])

            if speed:
                upload_speeds.append(speed)

        except Exception as e:

            error = f"{site['name']}: {e}"

            errors.append(error)

            print(error)

            print_separator()

    end_time = time.time()

    runtime = end_time - start_time

    avg_speed = 0

    if upload_speeds:
        avg_speed = sum(upload_speeds) / len(upload_speeds)

    summary = f"""
Backup Summary

Sites processed: {len(sites)}

Changed sites: {len(changed_sites)}
Changed list: {", ".join(changed_sites)}

Errors: {len(errors)}
{chr(10).join(errors)}

Average upload speed: {avg_speed:.2f} MB/s

Runtime: {runtime:.2f} seconds
"""

    print(summary)

    if changed_sites:
        send_email(config, summary)


if __name__ == "__main__":
    main()
