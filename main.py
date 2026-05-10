import os
import time
import shutil
import tempfile
from urllib.parse import urlparse
from datetime import datetime
import requests
from github import Auth, Github

# ------------------------------------------------------------
# HARDCODED CONFIGURATION (CHANGE THESE!)
# ------------------------------------------------------------
GITHUB_TOKEN = ""
REPO_OWNER = ""
REPO_NAME = "random"
DOWNLOAD_WORKFLOW_FILENAME = "download-split.yml"
DELETE_WORKFLOW_FILENAME = "delete-folder.yml"

MIN_SPEED_BYTES_PER_SEC = 200 * 1024   # 200 KB/s
SPEED_CHECK_INTERVAL = 1               # Check speed every 1 second
MAX_RETRIES = 2000
RETRY_DELAY = 1                        # Constant 1 second delay

# ------------------------------------------------------------
# Disable proxies
# ------------------------------------------------------------
os.environ['NO_PROXY'] = '*'
os.environ['no_proxy'] = '*'
for var in ['HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy']:
    os.environ.pop(var, None)

session = requests.Session()
session.trust_env = False
session.proxies = {'http': None, 'https': None}

# ------------------------------------------------------------
# GitHub client
# ------------------------------------------------------------
auth = Auth.Token(GITHUB_TOKEN)
g = Github(auth=auth)
repo = g.get_repo(f"{REPO_OWNER}/{REPO_NAME}")

# ------------------------------------------------------------
# Helper functions
# ------------------------------------------------------------
def generate_folder_name() -> str:
    return datetime.now().strftime("%m%d%H%M%S")

def is_workflow_busy(workflow_filename: str) -> bool:
    workflow = repo.get_workflow(workflow_filename)
    runs = workflow.get_runs()
    for run in runs:
        if run.status in ("queued", "in_progress"):
            return True
    return False

def trigger_workflow(workflow_filename: str, inputs: dict):
    """Trigger a workflow without waiting for completion."""
    if is_workflow_busy(workflow_filename):
        raise Exception(f"System is busy – workflow '{workflow_filename}' is already running. Please wait.")
    workflow = repo.get_workflow(workflow_filename)
    workflow.create_dispatch(ref=repo.default_branch, inputs=inputs)
    print(f"✅ Triggered workflow '{workflow_filename}' with inputs: {inputs}")

def wait_for_folder_to_appear(folder_name: str, timeout_seconds: int = 3600):
    """Wait until the folder exists in the default branch."""
    print(f"⏳ Waiting for folder '{folder_name}' to appear in the repository... (timeout: {timeout_seconds//60} minutes)")
    start_time = time.time()
    while time.time() - start_time < timeout_seconds:
        try:
            repo.get_contents(folder_name, ref=repo.default_branch)
            print(f"✅ Folder '{folder_name}' found in repository.")
            time.sleep(5)  # extra delay for GitHub to be ready
            latest_commit = repo.get_commits(sha=repo.default_branch)[0]
            return latest_commit.sha
        except:
            time.sleep(5)
            continue
    raise Exception(f"Folder '{folder_name}' did not appear within {timeout_seconds//60} minutes.")

# ------------------------------------------------------------
# Download functions (unchanged from working version)
# ------------------------------------------------------------
def get_remote_file_size(url):
    try:
        resp = session.head(url, allow_redirects=True, timeout=10)
        resp.raise_for_status()
        return int(resp.headers.get('Content-Length', 0))
    except:
        return 0

def download_file_with_speed_control(file_url, local_path,
                                     speed_threshold=MIN_SPEED_BYTES_PER_SEC,
                                     max_retries=MAX_RETRIES,
                                     retry_delay=RETRY_DELAY):
    os.makedirs(os.path.dirname(local_path), exist_ok=True)

    remote_size = get_remote_file_size(file_url)
    if remote_size == 0:
        print(f"  Warning: could not determine remote size for {file_url}")

    local_size = os.path.getsize(local_path) if os.path.exists(local_path) else 0
    if local_size == remote_size and remote_size > 0:
        print(f"  Already downloaded: {local_path}")
        return True

    downloaded = local_size
    for attempt in range(max_retries):
        try:
            headers = {}
            if downloaded > 0:
                headers['Range'] = f'bytes={downloaded}-'
                print(f"  Resuming from {downloaded / 1024:.1f} KB (attempt {attempt + 1}/{max_retries})")
            else:
                print(f"  Starting download (attempt {attempt + 1}/{max_retries})")

            response = session.get(file_url, headers=headers, stream=True, timeout=30)
            response.raise_for_status()

            total_size = remote_size
            if downloaded > 0 and response.status_code != 206:
                print("  Server does not support resume, restarting from zero.")
                downloaded = 0
                local_size = 0
                response = session.get(file_url, stream=True, timeout=30)
                response.raise_for_status()
                total_size = int(response.headers.get('Content-Length', 0))
            elif total_size == 0:
                total_size = int(response.headers.get('Content-Length', 0))

            mode = 'ab' if downloaded > 0 else 'wb'
            with open(local_path, mode) as f:
                bytes_last_check = 0
                last_check_time = time.time()
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)
                        now = time.time()
                        elapsed = now - last_check_time
                        if elapsed >= 1.0:
                            speed = (downloaded - bytes_last_check) / elapsed
                            bytes_last_check = downloaded
                            last_check_time = now
                            percent = (downloaded / total_size) * 100 if total_size > 0 else 0
                            print(
                                f"    {percent:.1f}%  |  Speed: {speed / 1024:.1f} KB/s  |  Downloaded: {downloaded / 1024:.1f} KB / {total_size / 1024:.1f} KB",
                                end='\r')
                            if speed < speed_threshold and downloaded < total_size:
                                print(
                                    f"\n  ⚠️ Speed dropped to {speed / 1024:.1f} KB/s (< {speed_threshold / 1024:.0f} KB/s). Pausing and resuming in {retry_delay}s...")
                                raise Exception("Speed below threshold")
                print(f"\n  ✅ Completed: {local_path} ({downloaded / 1024:.1f} KB)")
                return True

        except Exception as e:
            print(f"\n  ❌ Error: {e}")
            if attempt < max_retries - 1:
                time.sleep(retry_delay)
                local_size = os.path.getsize(local_path) if os.path.exists(local_path) else 0
                downloaded = local_size
                continue
            else:
                print(f"  ❌ Failed after {max_retries} attempts: {local_path}")
                return False
    return False

def download_folder(commit_sha, folder_name, output_dir, original_url):
    parsed = urlparse(original_url)
    original_filename = os.path.basename(parsed.path) or "downloaded_file"
    output_path = os.path.join(output_dir, original_filename)

    tree_url = f"https://api.github.com/repos/{REPO_OWNER}/{REPO_NAME}/git/trees/{commit_sha}"
    params = {"recursive": "1"}
    headers = {"Authorization": f"token {GITHUB_TOKEN}"}
    resp = session.get(tree_url, headers=headers, params=params)
    resp.raise_for_status()
    tree = resp.json()["tree"]

    folder_prefix = f"{folder_name}/"
    entries = [e for e in tree if e["path"].startswith(folder_prefix) and e["type"] == "blob"]
    if not entries:
        raise Exception("No files found in folder")

    has_chunks = any("chunks/" in e["path"] for e in entries)

    if has_chunks:
        temp_dir = tempfile.mkdtemp()
        try:
            chunk_files = []
            for entry in entries:
                if "chunks/" in entry["path"] and entry["size"] > 0:
                    chunk_name = os.path.basename(entry["path"])
                    chunk_path = os.path.join(temp_dir, chunk_name)
                    raw_url = f"https://raw.githubusercontent.com/{REPO_OWNER}/{REPO_NAME}/{commit_sha}/{entry['path']}"
                    print(f"Downloading chunk: {chunk_name}")
                    if not download_file_with_speed_control(raw_url, chunk_path):
                        raise Exception(f"Failed to download chunk {chunk_name}")
                    chunk_files.append((chunk_name, chunk_path))
            chunk_files.sort(key=lambda x: x[0])
            print(f"🧩 Reassembling {len(chunk_files)} chunks into {original_filename}")
            with open(output_path, "wb") as out_file:
                for _, chunk_path in chunk_files:
                    with open(chunk_path, "rb") as cf:
                        out_file.write(cf.read())
            print(f"✅ Reassembled file saved as: {output_path}")
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)
    else:
        for entry in entries:
            if "chunks/" not in entry["path"] and entry["type"] == "blob":
                raw_url = f"https://raw.githubusercontent.com/{REPO_OWNER}/{REPO_NAME}/{commit_sha}/{entry['path']}"
                if download_file_with_speed_control(raw_url, output_path):
                    print(f"✅ Single file saved as: {output_path}")
                else:
                    raise Exception("Download failed")
                return
        raise Exception("No single file found in folder")

# ------------------------------------------------------------
# Main processing
# ------------------------------------------------------------
def process_url(file_url: str, output_dir: str) -> None:
    folder_name = generate_folder_name()
    print(f"\n{'='*60}\nProcessing: {file_url}\nFolder name: {folder_name}\n{'='*60}")

    # 1. Trigger download workflow (no waiting)
    trigger_workflow(DOWNLOAD_WORKFLOW_FILENAME, {"file_url": file_url, "folder_name": folder_name})

    # 2. Wait for the folder to appear
    commit_sha = wait_for_folder_to_appear(folder_name)

    # 3. Download the folder contents
    download_folder(commit_sha, folder_name, output_dir, file_url)

    # 4. Trigger delete workflow (fire and forget)
    trigger_workflow(DELETE_WORKFLOW_FILENAME, {"folder_name": folder_name})
    print(f"🗑  Delete workflow triggered – folder will be removed shortly.\n")

    print(f"✅ Completed {file_url}\n")

def main():
    urls_input = input("Enter one or more file URLs (comma separated): ").strip()
    urls = [url.strip() for url in urls_input.split(",") if url.strip()]
    if not urls:
        print("No URLs provided.")
        return
    output_dir = input("Enter output directory (leave blank for current directory): ").strip()
    if not output_dir:
        output_dir = os.getcwd()
    os.makedirs(output_dir, exist_ok=True)

    for url in urls:
        try:
            process_url(url, output_dir)
        except Exception as e:
            print(f"❌ Failed to process {url}: {e}")

    print("\n🎉 All done!")

if __name__ == "__main__":
    main()
