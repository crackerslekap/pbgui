import streamlit as st
import pbgui_help
import json
import psutil
import sys
import platform
import subprocess
import glob
import configparser
import time
import multiprocessing
from Exchange import Exchange
from PBCoinData import CoinData
from pbgui_func import pb7dir, pb7venv, PBGDIR, load_symbols_from_ini, error_popup, info_popup, get_navi_paths, replace_special_chars
import uuid
from pathlib import Path, PurePath
from User import Users
import shutil
import datetime
import BacktestV7
from Config import ConfigV7, Bounds, Logging
import logging
import os
import fnmatch
import re
import statistics

class OptimizeV7QueueItem:
    def __init__(self):
        self.name = None
        self.filename = None
        self.json = None
        self.exchange = None
        self.starting_config = False
        self.log = None
        self.log_show = False
        self.pid = None
        self.pidfile = None
        self._progress_history = []  # List of (timestamp, progress_value) tuples
        self._start_time = None

    def remove(self):
        self.stop()
        file = Path(f'{PBGDIR}/data/opt_v7_queue/{self.filename}.json')
        file.unlink(missing_ok=True)
        self.log.unlink(missing_ok=True)
        self.pidfile.unlink(missing_ok=True)

    def load_log(self, log_size: int = 50):
        if self.log:
            if self.log.exists():
                # Open the file in binary mode to handle raw bytes
                with open(self.log, 'rb') as f:
                    # Move the pointer to the last log_size KB (100 * 1024 bytes)
                    f.seek(0, 2)  # Move to the end of the file
                    file_size = f.tell()
                    # Ensure that we don't try to read more than the file size
                    start_pos = max(file_size - log_size * 1024, 0)
                    f.seek(start_pos)
                    # Read the last 100 KB (or less if the file is smaller)
                    return f.read().decode('utf-8', errors='ignore')  # Decode and ignore errors

    @st.fragment
    def view_log(self):
        col1, col2, col3 = st.columns([1,1,8], vertical_alignment="bottom")
        with col1:
            st.checkbox("Reverse", value=True, key=f'reverse_view_log_{self.filename}')
        with col2:
            st.selectbox("view last kB", [50, 100, 250, 500, 1000, 2000, 5000, 10000, 100000], key=f'size_view_log_{self.filename}')
        with col3:
            if st.button(":material/refresh:", key=f'refresh_view_log_{self.filename}'):
                st.rerun(scope="fragment")
        logfile = self.load_log(st.session_state[f'size_view_log_{self.filename}'])
        if logfile:
            if st.session_state[f'reverse_view_log_{self.filename}']:
                logfile = '\n'.join(logfile.split('\n')[::-1])
        with st.container(height=1200):
            st.code(logfile)

    def status(self):
        if self.is_optimizing():
            return "optimizing..."
        if self.is_running():
            return "running"
        if self.is_finish():
            return "complete"
        if self.is_error():
            return "error"
        else:
            return "not started"

    def is_existing(self):
        if Path(f'{PBGDIR}/data/opt_v7_queue/{self.filename}.json').exists():
            return True
        return False

    def is_running(self):
        self.load_pid()
        try:
            if self.pid and psutil.pid_exists(self.pid) and any(sub.lower().endswith("optimize.py") for sub in psutil.Process(self.pid).cmdline()):
                return True
        except psutil.NoSuchProcess:
            pass
        except psutil.AccessDenied:
            pass
        return False

    def is_finish(self):
        log = self.load_log(log_size=1000)
        if log:
            if "successfully processed optimize_results" in log or "Optimization complete" in log:
                return True
            else:
                return False
        else:
            return False

    def is_error(self):
        log = self.load_log(log_size=1000)
        if log:
            if "successfully processed optimize_results" in log or "Optimization complete" in log:
                return False
            else:
                return True
        else:
            return False

    def is_optimizing(self):
        if self.is_running():
            log = self.load_log(log_size=1000)
            if log:
                if "Optimization complete" in log:
                    return False
                elif "Initial population size" in log:
                    return True
            else:
                return False
        return False

    def is_fetching(self):
        """Check if currently fetching/downloading data"""
        if self.is_running() and not self.is_optimizing() and not self.is_finish():
            log = self.load_log(log_size=500)
            if log:
                # Look for fetching/downloading indicators
                fetch_indicators = [
                    "downloading", "fetching", "Download", "Fetch",
                    "loading data", "preparing data", "initializing"
                ]
                log_lower = log.lower()
                if any(ind.lower() in log_lower for ind in fetch_indicators):
                    # Check if optimization hasn't started yet
                    if "Initial population size" not in log and "Optimization complete" not in log:
                        return True
            # If running but not optimizing and no finish, likely fetching
            return True
        return False

    def _extract_iteration_progress(self, log: str | None) -> tuple[float | None, int | None, int | None]:
        """Extract iteration progress: (progress_pct, current_iter, total_iters)"""
        if not log:
            return None, None, None
        
        # Try to find iteration patterns like "iteration 100/1000" or "iter 50 of 200"
        patterns = [
            r'iteration\s+(\d+)\s*[/]\s*(\d+)',
            r'iter\s+(\d+)\s+of\s+(\d+)',
            r'iter\s+(\d+)\s*[/]\s*(\d+)',
            r'(\d+)\s*/\s*(\d+)\s+iterations?',
            r'generation\s+(\d+)\s*[/]\s*(\d+)',
        ]
        
        for pattern in patterns:
            for line in reversed(log.splitlines()[-100:]):
                match = re.search(pattern, line, re.IGNORECASE)
                if match:
                    current = int(match.group(1))
                    total = int(match.group(2))
                    if total > 0:
                        pct = min((current / total) * 100, 100)
                        return pct, current, total
        
        # Try to extract from config if available
        try:
            if self.json and Path(self.json).exists():
                with open(self.json, 'r') as f:
                    config_data = json.load(f)
                    if 'optimize' in config_data and 'iters' in config_data['optimize']:
                        total_iters = config_data['optimize']['iters']
                        # Try to find current iteration in log
                        for line in reversed(log.splitlines()[-100:]):
                            iter_match = re.search(r'(\d+)\s*(?:th|st|nd|rd)?\s+iteration', line, re.IGNORECASE)
                            if iter_match:
                                current = int(iter_match.group(1))
                                if current <= total_iters:
                                    pct = min((current / total_iters) * 100, 100)
                                    return pct, current, total_iters
        except:
            pass
        
        return None, None, None

    def _extract_progress_percent(self, log: str | None) -> float | None:
        if not log:
            return None
        for line in reversed(log.splitlines()[-40:]):
            match = re.search(r'(\d{1,3})(?:\.\d+)?\s*%', line)
            if match:
                pct = float(match.group(1))
                if 0 <= pct <= 100:
                    return pct
        return None

    def _calculate_eta(self, current_progress: float, progress_rate: float | None) -> str | None:
        """Calculate ETA based on progress rate"""
        if progress_rate is None or progress_rate <= 0:
            return None
        
        remaining = 100 - current_progress
        if remaining <= 0:
            return "Complete"
        
        seconds_remaining = remaining / progress_rate
        if seconds_remaining < 60:
            return f"{int(seconds_remaining)}s"
        elif seconds_remaining < 3600:
            minutes = int(seconds_remaining / 60)
            return f"{minutes}m"
        else:
            hours = int(seconds_remaining / 3600)
            minutes = int((seconds_remaining % 3600) / 60)
            return f"{hours}h {minutes}m"

    def progress(self):
        """Returns (progress_value, label, stage, eta)"""
        current_time = time.time()
        
        # Initialize start time if not set and process is running
        if self._start_time is None and self.is_running():
            self._start_time = current_time
        
        log = self.load_log(log_size=500)
        
        # Determine stage
        if self.is_finish():
            stage = "complete"
            progress_pct = 100.0
            label = "Complete"
            eta = None
        elif self.is_fetching():
            stage = "fetching"
            # Estimate fetching progress based on time (rough estimate)
            if self._start_time:
                elapsed = current_time - self._start_time
                # Assume fetching takes 5-15% of total time, estimate progress
                progress_pct = min(elapsed / 300 * 10, 15)  # Max 15% for fetching
            else:
                progress_pct = 5.0
            label = "Fetching data..."
            eta = None  # Hard to estimate fetching time
        elif self.is_optimizing():
            stage = "optimizing"
            # Try to extract iteration progress
            iter_pct, current_iter, total_iters = self._extract_iteration_progress(log)
            if iter_pct is not None:
                progress_pct = iter_pct
                label = f"Iteration {current_iter}/{total_iters}"
                # Calculate ETA based on iteration rate
                if len(self._progress_history) >= 2:
                    recent = self._progress_history[-5:]
                    if len(recent) >= 2:
                        time_diff = recent[-1][0] - recent[0][0]
                        progress_diff = recent[-1][1] - recent[0][1]
                        if time_diff > 0 and progress_diff > 0:
                            rate = progress_diff / time_diff  # % per second
                            eta = self._calculate_eta(progress_pct, rate)
                        else:
                            eta = None
                    else:
                        eta = None
                else:
                    eta = None
            else:
                # Fallback: try to extract percentage from log
                pct = self._extract_progress_percent(log)
                if pct is not None:
                    progress_pct = pct
                else:
                    progress_pct = 50.0  # Default midpoint for optimizing
                label = "Optimizing..."
                eta = None
        elif self.is_running():
            stage = "starting"
            progress_pct = 10.0
            label = "Starting..."
            eta = None
        elif self.is_error():
            stage = "error"
            progress_pct = 0.0
            label = "Error"
            eta = None
        else:
            stage = "queued"
            progress_pct = 0.0
            label = "Queued"
            eta = None
        
        # Update progress history for rate calculation
        if self.is_running() and progress_pct > 0:
            self._progress_history.append((current_time, progress_pct))
            # Keep only last 20 entries
            if len(self._progress_history) > 20:
                self._progress_history = self._progress_history[-20:]
        
        # Reset start time if process stopped
        if not self.is_running() and self._start_time:
            self._start_time = None
            self._progress_history = []
        
        progress_value = min(progress_pct, 100) / 100
        
        # Format label with ETA if available
        if eta:
            label = f"{label} (ETA: {eta})"
        
        return progress_value, label, stage

    def stop(self):
        if self.is_running():
            parent = psutil.Process(self.pid)
            children = parent.children(recursive=True)
            children.append(parent)
            for p in children:
                try:
                    p.kill()
                except psutil.NoSuchProcess:
                    pass


    def load_pid(self):
        if self.pidfile.exists():
            with open(self.pidfile) as f:
                pid = f.read()
                self.pid = int(pid) if pid.isnumeric() else None

    def save_pid(self):
        with open(self.pidfile, 'w') as f:
            f.write(str(self.pid))

    def run(self):
        if not self.is_finish() and not self.is_running():
            old_os_path = os.environ.get('PATH', '')
            new_os_path = os.path.dirname(pb7venv()) + os.pathsep + old_os_path
            os.environ['PATH'] = new_os_path
            if self.starting_config:
                cmd = [pb7venv(), '-u', PurePath(f'{pb7dir()}/src/optimize.py'), '-t', str(PurePath(f'{self.json}')), str(PurePath(f'{self.json}'))]
            else:
                cmd = [pb7venv(), '-u', PurePath(f'{pb7dir()}/src/optimize.py'), str(PurePath(f'{self.json}'))]
            log = open(self.log,"w")
            if platform.system() == "Windows":
                creationflags = subprocess.DETACHED_PROCESS
                creationflags |= subprocess.CREATE_NO_WINDOW
                btm = subprocess.Popen(cmd, stdout=log, stderr=log, cwd=pb7dir(), text=True, creationflags=creationflags)
            else:
                btm = subprocess.Popen(cmd, stdout=log, stderr=log, cwd=pb7dir(), text=True, start_new_session=True)
            self.pid = btm.pid
            self.save_pid()
            os.environ['PATH'] = old_os_path

class OptimizeV7Queue:
    def __init__(self):
        self.items = []
        self.d = []
        self.sort = "Time"
        self.sort_order = True
        pb_config = configparser.ConfigParser()
        pb_config.read('pbgui.ini')
        if not pb_config.has_section("optimize_v7"):
            pb_config.add_section("optimize_v7")
            pb_config.set("optimize_v7", "autostart", "False")
            with open('pbgui.ini', 'w') as f:
                pb_config.write(f)
        self._autostart = eval(pb_config.get("optimize_v7", "autostart"))
        self.load_sort_queue()
        if self._autostart:
            self.run()

    @property
    def autostart(self):
        return self._autostart

    @autostart.setter
    def autostart(self, new_autostart):
        self._autostart = new_autostart
        pb_config = configparser.ConfigParser()
        pb_config.read('pbgui.ini')
        pb_config.set("optimize_v7", "autostart", str(self._autostart))
        with open('pbgui.ini', 'w') as f:
            pb_config.write(f)
        if self._autostart:
            self.run()
        else:
            self.stop()

    def add(self, qitem : OptimizeV7QueueItem):
        for index, item in enumerate(self.items):
            if item.filename == qitem.filename:
                return
        self.items.append(qitem)

    def remove_finish(self, all : bool = False):
        if all:
            self.stop()
        for item in self.items[:]:
            if item.is_finish():
                item.remove()
                self.items.remove(item)
            else:
                if all:
                    item.stop()
                    item.remove()
                    self.items.remove(item)
        if self._autostart:
            self.run()
        self.refresh()

    def remove_selected(self):
        ed_key = st.session_state.ed_key
        ed = st.session_state[f'view_opt_v7_queue_{ed_key}']
        for row in ed["edited_rows"]:
            if "delete" in ed["edited_rows"][row]:
                if ed["edited_rows"][row]["delete"]:
                    self.d[row]["item"].remove()
                    self.items.remove(self.d[row]["item"])
        self.refresh()

    def running(self):
        for item in self.items:
            if item.is_running():
                return True
        return False

    def downloading(self):
        for item in self.items:
            if item.is_running() and not item.is_optimizing():
                return True
        return False
        
    def load(self):
        dest = Path(f'{PBGDIR}/data/opt_v7_queue')
        p = str(Path(f'{dest}/*.json'))
        items = glob.glob(p)
        for item in items:
            with open(item, "r", encoding='utf-8') as f:
                q_config = json.load(f)
                qitem = OptimizeV7QueueItem()
                qitem.name = q_config["name"]
                qitem.filename = q_config["filename"]
                qitem.json = q_config["json"]
                config = OptimizeV7Item(qitem.json)
                qitem.exchange = q_config["exchange"]
                qitem.starting_config = config.config.pbgui.starting_config
                qitem.log = Path(f'{PBGDIR}/data/opt_v7_queue/{qitem.filename}.log')
                qitem.pidfile = Path(f'{PBGDIR}/data/opt_v7_queue/{qitem.filename}.pid')
                self.add(qitem)
        # Remove items that are not existing anymore
        for item in self.items[:]:
            if not Path(f'{PBGDIR}/data/opt_v7_queue/{item.filename}.json').exists():
                item.remove()
                self.items.remove(item)

    def run(self):
        if not self.is_running():
            cmd = [sys.executable, '-u', PurePath(f'{PBGDIR}/OptimizeV7.py')]
            dest = Path(f'{PBGDIR}/data/logs')
            if not dest.exists():
                dest.mkdir(parents=True)
            logfile = Path(f'{dest}/OptimizeV7.log')
            if logfile.exists():
                if logfile.stat().st_size >= 1048576:
                    logfile.replace(f'{str(logfile)}.old')
            log = open(logfile,"a")
            if platform.system() == "Windows":
                creationflags = subprocess.DETACHED_PROCESS
                creationflags |= subprocess.CREATE_NO_WINDOW
                subprocess.Popen(cmd, stdout=log, stderr=log, cwd=PBGDIR, text=True, creationflags=creationflags)
            else:
                subprocess.Popen(cmd, stdout=log, stderr=log, cwd=PBGDIR, text=True, start_new_session=True)

    def stop(self):
        if self.is_running():
            self.pid().kill()

    def is_running(self):
        if self.pid():
            return True
        return False

    def pid(self):
        for process in psutil.process_iter():
            try:
                cmdline = process.cmdline()
            except psutil.AccessDenied:
                continue
            if any("OptimizeV7.py" in sub for sub in cmdline):
                return process

    def refresh(self):
        # Remove items from d that are not in items anymore
        self.d = [item for item in self.d if item.get('filename') in [i.filename for i in self.items]]
        # Add items to d that are in items but not in d
        for item in self.items:
            if not any(d_item.get('filename') == item.filename for d_item in self.d):
                self.d.append({
                    'id': len(self.d),
                    'run': item.is_running(),
                    'Status': item.status(),
                    'edit': False,
                    'log': item.log_show,
                    'delete': False,
                    'starting_config': item.starting_config,
                    'name': item.name,
                    'filename': item.filename,
                    'Time': datetime.datetime.fromtimestamp(Path(f'{PBGDIR}/data/opt_v7_queue/{item.filename}.json').stat().st_mtime),
                    'exchange': item.exchange,
                    'finish': item.is_finish(),
                    'item': item,
                })
        # Update status of all items in d
        for row in self.d:
            for item in self.items:
                if row['filename'] == item.filename:
                    row['run'] = item.is_running()
                    row['Status'] = item.status()
                    row['log'] = item.log_show
                    row['finish'] = item.is_finish()

    def view(self):
        if not self.items:
            self.load()
            self.refresh()
        if not "ed_key" in st.session_state:
            st.session_state.ed_key = 0
        ed_key = st.session_state.ed_key
        if f'view_opt_v7_queue_{ed_key}' in st.session_state:
            ed = st.session_state[f'view_opt_v7_queue_{ed_key}']
            for row in ed["edited_rows"]:
                if "run" in ed["edited_rows"][row]:
                    if ed["edited_rows"][row]["run"]:
                        self.d[row]["item"].run()
                    else:
                        self.d[row]["item"].stop()
                    self.refresh()
                if "edit" in ed["edited_rows"][row]:
                    st.session_state.opt_v7 = OptimizeV7Item(f'{PBGDIR}/data/opt_v7/{self.d[row]["item"].name}.json')
                    del st.session_state.opt_v7_queue
                    st.rerun()
                if "log" in ed["edited_rows"][row]:
                    self.d[row]["item"].log_show = ed["edited_rows"][row]["log"]
                    self.d[row]["log"] = ed["edited_rows"][row]["log"]
        column_config = {
            # "id": None,
            "run": st.column_config.CheckboxColumn('Start/Stop', default=False),
            "edit": st.column_config.CheckboxColumn('Edit'),
            "log": st.column_config.CheckboxColumn(label="View Logfile"),
            "delete": st.column_config.CheckboxColumn(label="Delete"),
            "item": None,
            }
        #Display Queue
        height = 36+(len(self.d))*35
        if height > 1000: height = 1016
        if "sort_opt_v7_queue" in st.session_state:
            if st.session_state.sort_opt_v7_queue != self.sort:
                self.sort = st.session_state.sort_opt_v7_queue
                self.save_sort_queue()
        else:
            st.session_state.sort_opt_v7_queue = self.sort
        if "sort_opt_v7_queue_order" in st.session_state:
            if st.session_state.sort_opt_v7_queue_order != self.sort_order:
                self.sort_order = st.session_state.sort_opt_v7_queue_order
                self.save_sort_queue()
        else:
            st.session_state.sort_opt_v7_queue_order = self.sort_order
        # Display sort options
        col1, col2 = st.columns([1, 9], vertical_alignment="bottom")
        with col1:
            st.selectbox("Sort by:", ['Time', 'name', 'Status', 'exchange', 'finish'], key=f'sort_opt_v7_queue', index=0)
        with col2:
            st.checkbox("Reverse", value=True, key=f'sort_opt_v7_queue_order')
        self.d = sorted(self.d, key=lambda x: x[st.session_state[f'sort_opt_v7_queue']], reverse=st.session_state[f'sort_opt_v7_queue_order'])
        st.data_editor(data=self.d, height="auto", key=f'view_opt_v7_queue_{ed_key}', hide_index=None, column_order=None, column_config=column_config, disabled=['id','filename','starting_config','name','finish','running'])
        for item in self.items:
            if item.log_show:
                item.view_log()
        active = [item for item in self.items if item.is_running() or not item.is_finish()]
        if active:
            st.markdown("#### Progress")
            for item in active:
                value, label, stage = item.progress()
                name = item.name or item.filename
                
                # Show stage-specific progress bars
                if stage == "fetching":
                    st.progress(value)
                    st.caption(f"📥 {name}: {label}")
                elif stage == "optimizing":
                    st.progress(value)
                    st.caption(f"⚙️ {name}: {label}")
                elif stage == "complete":
                    st.progress(value)
                    st.caption(f"✅ {name}: {label}")
                elif stage == "error":
                    st.progress(value)
                    st.caption(f"❌ {name}: {label}")
                else:
                    st.progress(value)
                    st.caption(f"{name}: {label}")

    def load_sort_queue(self):
        pb_config = configparser.ConfigParser()
        pb_config.read('pbgui.ini')
        self.sort = pb_config.get("optimize_v7", "sort_queue") if pb_config.has_option("optimize_v7", "sort_queue") else "Time"
        self.sort_order = eval(pb_config.get("optimize_v7", "sort_queue_order")) if pb_config.has_option("optimize_v7", "sort_queue_order") else True

    def save_sort_queue(self):
        pb_config = configparser.ConfigParser()
        pb_config.read('pbgui.ini')
        if not pb_config.has_section("optimize_v7"):
            pb_config.add_section("optimize_v7")
        pb_config.set("optimize_v7", "sort_queue", str(self.sort))
        pb_config.set("optimize_v7", "sort_queue_order", str(self.sort_order))
        with open('pbgui.ini', 'w') as f:
            pb_config.write(f)

class OptimizeV7Results:
    def __init__(self):
        self.results_path = Path(f'{pb7dir()}/optimize_results')
        self.selected_analysis = "analyses_combined"
        self.results_new = []
        self.results_d = []
        self.sort_results = "Result Time"
        self.sort_results_order = True
        self.paretos = []
        self.filter = ""
        self.initialize()
        self.load_sort_results()
    
    def initialize(self):
        self.find_results()
    
    def find_results(self):
        if self.results_path.exists():
            p = str(self.results_path) + "/*/all_results.bin"
            self.results_new = glob.glob(p, recursive=False)
            self.results_d = []
    
    def find_result_name_new(self, result_file):
        p = str(PurePath(result_file).parent / "pareto" / "*.json")
        files = glob.glob(p, recursive=False)
        if files:
            config = ConfigV7(files[0])
            config.load_config()
            result_time = Path(files[0]).stat().st_mtime
            return config.backtest.base_dir.split("/")[-1], result_time
        else:
            return None, None

    def view_results(self):
        # Init
        if not "ed_key" in st.session_state:
            st.session_state.ed_key = 0
        ed_key = st.session_state.ed_key

        if "select_opt_v7_result_filter" in st.session_state:
            if st.session_state.select_opt_v7_result_filter != self.filter:
                self.filter = st.session_state.select_opt_v7_result_filter
                self.results_new = []
                self.results_d = []
                self.find_results()
        else:
            st.session_state.select_opt_v7_result_filter = self.filter

        # Remove results that are not in the filter
        if not self.filter == "":
            for result in self.results_new.copy():
                name, result_time = self.find_result_name_new(result)
                if not name:
                    self.results_new.remove(result)
                    continue
                if not fnmatch.fnmatch(name.lower(), self.filter.lower()):
                    self.results_new.remove(result)

        st.text_input("Filter by Optimize Name", value="", help=pbgui_help.smart_filter, key="select_opt_v7_result_filter")

        if not self.results_d:
            for id, opt in enumerate(self.results_new):
                name, result_time = self.find_result_name_new(opt)
                if name:
                    result = PurePath(opt).parent.name
                    self.results_d.append({
                        'id': id,
                        'Name': name,
                        'Result Time': datetime.datetime.fromtimestamp(result_time),
                        'view': False,
                        '3d plot': False,
                        'delete' : False,
                        'Result': result,
                        'index': opt,
                    })
        column_config_new = {
            "id": None,
            "edit": st.column_config.CheckboxColumn(label="Edit"),
            "view": st.column_config.CheckboxColumn(label="View Paretos"),
            "Result Time": st.column_config.DatetimeColumn(format="YYYY-MM-DD HH:mm:ss"),
            "Result": st.column_config.TextColumn(label="Result Directory", width="50px"),
        }
        if "sort_opt_v7_results" in st.session_state:
            if st.session_state.sort_opt_v7_results != self.sort_results:
                self.sort_results = st.session_state.sort_opt_v7_results
                self.save_sort_results()
        else:
            st.session_state.sort_opt_v7_results = self.sort_results
        if "sort_opt_v7_results_order" in st.session_state:
            if st.session_state.sort_opt_v7_results_order != self.sort_results_order:
                self.sort_results_order = st.session_state.sort_opt_v7_results_order
                self.save_sort_results()
        else:
            st.session_state.sort_opt_v7_results_order = self.sort_results_order
        # Display sort options
        col1, col2 = st.columns([1, 9], vertical_alignment="bottom")
        with col1:
            st.selectbox("Sort by:", ['Result Time', 'Name'], key=f'sort_opt_v7_results', index=0)
        with col2:
            st.checkbox("Reverse", value=True, key=f'sort_opt_v7_results_order')
        # Sort results
        self.results_d = sorted(self.results_d, key=lambda x: x[st.session_state[f'sort_opt_v7_results']], reverse=st.session_state[f'sort_opt_v7_results_order'])
        #Display optimizes
        st.data_editor(data=self.results_d, height=36+(len(self.results_d))*35, key=f'select_optresults_new_{st.session_state.ed_key}', hide_index=None, column_order=None, column_config=column_config_new, disabled=['id','name','index'])
        if f'select_optresults_new_{st.session_state.ed_key}' in st.session_state:
            ed = st.session_state[f'select_optresults_new_{st.session_state.ed_key}']
            for row in ed["edited_rows"]:
                if "view" in ed["edited_rows"][row]:
                    if ed["edited_rows"][row]["view"]:
                        if self.results_d[row]["Result"]:
                            st.session_state.opt_v7_pareto = self.results_d[row]["index"]
                            st.session_state.opt_v7_pareto_name = self.results_d[row]["Name"]
                            st.session_state.opt_v7_pareto_directory = self.results_d[row]["Result"]
                            st.rerun()
                if "3d plot" in ed["edited_rows"][row]:
                    if ed["edited_rows"][row]["3d plot"]:
                        self.run_3d_plot(self.results_d[row]["index"])
                        st.session_state.ed_key += 1

    def load_sort_results(self):
        pb_config = configparser.ConfigParser()
        pb_config.read('pbgui.ini')
        self.sort_results = pb_config.get("optimize_v7", "sort_results") if pb_config.has_option("optimize_v7", "sort_results") else "Result Time"
        self.sort_results_order = eval(pb_config.get("optimize_v7", "sort_results_order")) if pb_config.has_option("optimize_v7", "sort_results_order") else True

    def save_sort_results(self):
        pb_config = configparser.ConfigParser()
        pb_config.read('pbgui.ini')
        if not pb_config.has_section("optimize_v7"):
            pb_config.add_section("optimize_v7")
        pb_config.set("optimize_v7", "sort_results", str(self.sort_results))
        pb_config.set("optimize_v7", "sort_results_order", str(self.sort_results_order))
        with open('pbgui.ini', 'w') as f:
            pb_config.write(f)

    def run_3d_plot(self, index):
        # run 3d plot
        directory = Path(index).parent / "pareto"
        cmd = [pb7venv(), '-u', PurePath(f'{pb7dir()}/src/pareto_store.py'), str(directory)]
        if platform.system() == "Windows":
            creationflags = subprocess.CREATE_NO_WINDOW
            result = subprocess.run(cmd, capture_output=True, cwd=pb7dir(), text=True, creationflags=creationflags)
        else:
            result = subprocess.run(cmd, capture_output=True, cwd=pb7dir(), text=True, start_new_session=True)
        info_popup(f"3D Plot Generated {result.stdout}")

    def load_paretos(self, index):
        self.paretos = []
        paretos_path = PurePath(f'{index}').parent / "pareto"
        if Path(paretos_path).exists():
            # find all json files in paretos_path
            p = str(paretos_path) + "/*.json"
            paretos = glob.glob(p, recursive=False)
        for pareto in paretos:
            with open(pareto, "r", encoding='utf-8') as f:
                pareto_data = json.load(f)
                pareto_data['index_filename'] = pareto
                self.paretos.append(pareto_data)
        # sort by index_filename
        self.paretos.sort(key=lambda x: x['index_filename'])

    def view_pareto(self, index):
        if not self.paretos:
            self.load_paretos(index)
        select_analysis = []
        if "analyses_combined" in self.paretos[0]:
            select_analysis.append("analyses_combined")
        if "analyses" in self.paretos[0]:
            for analyse in self.paretos[0]["analyses"]:
                select_analysis.append(analyse)
        def clear_paretos():
            if "d_paretos" in st.session_state:
                del st.session_state.d_paretos
        col1, col2 = st.columns([1, 3], gap="small")
        with col1:
            if "opt_v7_pareto_select_analysis" in st.session_state:
                if st.session_state.opt_v7_pareto_select_analysis != self.selected_analysis:
                    self.selected_analysis = st.session_state.opt_v7_pareto_select_analysis
            else:
                st.session_state.opt_v7_pareto_select_analysis = self.selected_analysis
            st.selectbox('analyses', options=select_analysis, key="opt_v7_pareto_select_analysis", on_change=clear_paretos)
        if not "d_paretos" in st.session_state:
            d = []
            for id, pareto in enumerate(self.paretos):
                if select_analysis:
                    name = pareto["index_filename"].split("/")[-1]
                    if st.session_state.opt_v7_pareto_select_analysis == "analyses_combined":
                        analysis = pareto["analyses_combined"]
                        d.append({
                            'Select': False,
                            'id': id,
                            'view': False,
                            'adg': analysis["adg_max"],
                            'mdg': analysis["mdg_max"],
                            'drawdown_worst': analysis["drawdown_worst_max"],
                            'gain': analysis["gain_max"],
                            'loss_profit_ratio': analysis["loss_profit_ratio_max"],
                            'position_held_hours_max': analysis["position_held_hours_max_max"],
                            'sharpe_ratio': analysis["sharpe_ratio_max"],
                            'Name': name,
                            'file': pareto["index_filename"],
                        })
                    else:
                        analysis = pareto["analyses"][st.session_state.opt_v7_pareto_select_analysis]
                        d.append({
                            'Select': False,
                            'id': id,
                            'view': False,
                            'adg': analysis["adg"],
                            'mdg': analysis["mdg"],
                            'drawdown_worst': analysis["drawdown_worst"],
                            'gain': analysis["gain"],
                            'loss_profit_ratio': analysis["loss_profit_ratio"],
                            'position_held_hours_max': analysis["position_held_hours_max"],
                            'sharpe_ratio': analysis["sharpe_ratio"],
                            'Name': name,
                            'file': pareto["index_filename"],
                        })
            st.session_state.d_paretos = d
        d_paretos = st.session_state.d_paretos
        column_config = {
            "id": None,
            "Select": st.column_config.CheckboxColumn(label="Select"),
            "file": None,
            "view": st.column_config.CheckboxColumn(label="View"),
            "delete": st.column_config.CheckboxColumn(label="Delete"),
            }
        #Display paretos
        height = 36+(len(d_paretos))*35
        if height > 1000: height = 1016
        st.data_editor(data=d_paretos, height="auto", key=f'select_paretos_{st.session_state.ed_key}', hide_index=None, column_order=None, column_config=column_config, disabled=['id','file'])
        if f'select_paretos_{st.session_state.ed_key}' in st.session_state:
            ed = st.session_state[f'select_paretos_{st.session_state.ed_key}']
            for row in ed["edited_rows"]:
                if "view" in ed["edited_rows"][row]:
                    if ed["edited_rows"][row]["view"]:
                        st.write(f"Pareto {d_paretos[row]['Name']}")
                        st.code(json.dumps(self.paretos[row], indent=4))

    def cleanup_bt_session_state(self):
        if "bt_v7_queue" in st.session_state:
            del st.session_state.bt_v7_queue
        if "bt_v7_results" in st.session_state:
            del st.session_state.bt_v7_results
        if "bt_v7_edit_symbol" in st.session_state:
            del st.session_state.bt_v7_edit_symbol
        if "config_v7_archives" in st.session_state:
            del st.session_state.config_v7_archives
        if "config_v7_config_archive" in st.session_state:
            del st.session_state.config_v7_config_archive

    def backtest_selected(self):
        if "d_paretos" in st.session_state:
            d_paretos = st.session_state.d_paretos
        else:
            return
        ed_key = st.session_state.ed_key
        ed = st.session_state[f'select_paretos_{st.session_state.ed_key}']
        # Get number of selected paretos
        selected_count = sum(1 for row in ed["edited_rows"] if "Select" in ed["edited_rows"][row] and ed["edited_rows"][row]["Select"])
        if selected_count == 0:
            error_popup("No paretos selected")
            return
        self.cleanup_bt_session_state()
        for row in ed["edited_rows"]:
            if "Select" in ed["edited_rows"][row]:
                if ed["edited_rows"][row]["Select"]:
                    backtest_name = d_paretos[row]["file"]
                    # run backtest on selected pareto
                    if selected_count == 1:
                        st.session_state.bt_v7 = BacktestV7.BacktestV7Item(backtest_name)
                        st.switch_page(get_navi_paths()["V7_BACKTEST"])
                    else:
                        bt_v7 = BacktestV7.BacktestV7Item(backtest_name)
                        bt_v7.save_queue()
        st.session_state.bt_v7_queue = BacktestV7.BacktestV7Queue()
        st.switch_page(get_navi_paths()["V7_BACKTEST"])
    
    def backtest_all(self):
        if "d_paretos" in st.session_state:
            d_paretos = st.session_state.d_paretos
        else:
            return
        for row in range(len(d_paretos)):
            backtest_name = d_paretos[row]["file"]
            # run backtest on selected pareto
            bt_v7 = BacktestV7.BacktestV7Item(backtest_name)
            bt_v7.save_queue()
        if "bt_v7_results" in st.session_state:
            del st.session_state.bt_v7_results
        if "bt_v7_edit_symbol" in st.session_state:
            del st.session_state.bt_v7_edit_symbol
        st.session_state.bt_v7_queue = BacktestV7.BacktestV7Queue()
        st.switch_page(get_navi_paths()["V7_BACKTEST"])


    def remove_selected_results(self):
        ed_key = st.session_state.ed_key
        if not self.results_d:
            return
        ed = st.session_state[f'select_optresults_new_{ed_key}']
        for row in ed["edited_rows"]:
            if "delete" in ed["edited_rows"][row]:
                if ed["edited_rows"][row]["delete"]:
                    directory = Path(self.results_d[row]["index"]).parent
                    shutil.rmtree(directory, ignore_errors=True)
        self.find_results()

    def remove_all_results(self):
        shutil.rmtree(self.results_path, ignore_errors=True)
        self.results_d = []
        self.results_new = []

class OptimizeV7Item:
    def __init__(self, optimize_file: str = None):
        self.name = ""
        self.log = None
        self.config = ConfigV7()
        self.users = Users()
        self.backtest_count:int = 0
        if optimize_file:
            self.name = PurePath(optimize_file).stem
            self.config.config_file = optimize_file
            self.config.load_config()
            if Path(optimize_file).exists():
                self.time = datetime.datetime.fromtimestamp(Path(optimize_file).stat().st_mtime)
            else:
                self.time = datetime.datetime.now()
        else:
            self.initialize()
        self._calculate_results()
        if "limits_data" in st.session_state:
            del st.session_state.limits_data

    def _calculate_results(self):
        if self.name:
            base_path = Path(f'{pb7dir()}/backtests/pbgui/{self.name}')
            p = str(Path(f'{base_path}/**/analysis.json'))
            files = glob.glob(p, recursive=True)
            self.backtest_count = len(files)
            
    def initialize(self):
        self.config.backtest.start_date = (datetime.date.today() - datetime.timedelta(days=365*4)).strftime("%Y-%m-%d")
        self.config.backtest.end_date = datetime.date.today().strftime("%Y-%m-%d")
        self.config.optimize.n_cpus = multiprocessing.cpu_count()
    
    def find_presets(self):
        dest = Path(f'{PBGDIR}/data/opt_v7_presets')
        p = str(Path(f'{dest}/*.json'))
        presets = glob.glob(p)
        presets = [PurePath(p).stem for p in presets]
        return presets
    
    def preset_load(self, preset):
        dest = Path(f'{PBGDIR}/data/opt_v7_presets')
        file = Path(f'{dest}/{preset}.json')
        self.config = ConfigV7()
        self.config.config_file = file
        self.config.load_config()
        self.name = PurePath(self.config.config_file).stem
        
        if "edit_opt_v7_name" in st.session_state:
            st.session_state.edit_opt_v7_name = self.name
        
    def preset_save(self) -> bool:
        if self.name == "":
            error_popup("Name is empty")
            return False
        
        dest = Path(f'{PBGDIR}/data/opt_v7_presets')
        if not dest.exists():
            dest.mkdir(parents=True)
        
        # Prevent creating directories with / in the name
        self.name = self.name.replace("/", "_")
        
        file = Path(f'{dest}/{self.name}.json')   
        self.config.config_file = file
        self.config.save_config()
        return True
    
    def preset_remove(self, preset):
        dest = Path(f'{PBGDIR}/data/opt_v7_presets')
        file = Path(f'{dest}/{preset}.json')
        file.unlink(missing_ok=True)
                
    # Exchanges
    @st.fragment
    def fragment_exchanges(self):
        if "edit_opt_v7_exchanges" in st.session_state:
            if st.session_state.edit_opt_v7_exchanges != self.config.backtest.exchanges:
                self.config.backtest.exchanges = st.session_state.edit_opt_v7_exchanges
                st.rerun()
        else:
            st.session_state.edit_opt_v7_exchanges = self.config.backtest.exchanges
        st.multiselect('Exchanges',["binance", "bybit", "gateio", "bitget"], key="edit_opt_v7_exchanges")

    # name
    @st.fragment
    def fragment_name(self):
        if "edit_opt_v7_name" in st.session_state:
            if st.session_state.edit_opt_v7_name != self.name:
                # Avoid creation of unwanted subfolders
                st.session_state.edit_opt_v7_name = replace_special_chars(st.session_state.edit_opt_v7_name)
                self.name = st.session_state.edit_opt_v7_name
                self.config.backtest.base_dir = f'backtests/pbgui/{self.name}'
        else:
            st.session_state.edit_opt_v7_name = self.name
        if not self.name:
            st.text_input(f":red[Optimize Name]",max_chars=64, key="edit_opt_v7_name")
        else:
            st.text_input("Optimize Name", max_chars=64, help=pbgui_help.task_name, key="edit_opt_v7_name")

    # start_data
    @st.fragment
    def fragment_start_date(self):
        if "edit_opt_v7_start_date" in st.session_state:
            if st.session_state.edit_opt_v7_start_date.strftime("%Y-%m-%d") != self.config.backtest.start_date:
                self.config.backtest.start_date = st.session_state.edit_opt_v7_start_date.strftime("%Y-%m-%d")
        else:
            st.session_state.edit_opt_v7_start_date = datetime.datetime.strptime(self.config.backtest.start_date, '%Y-%m-%d')
        st.date_input("start_date", format="YYYY-MM-DD", key="edit_opt_v7_start_date")

    # end_date
    @st.fragment
    def fragment_end_date(self):
        if "edit_opt_v7_end_date" in st.session_state:
            if st.session_state.edit_opt_v7_end_date.strftime("%Y-%m-%d") != self.config.backtest.end_date:
                self.config.backtest.end_date = st.session_state.edit_opt_v7_end_date.strftime("%Y-%m-%d")
        else:
            st.session_state.edit_opt_v7_end_date = datetime.datetime.strptime(self.config.backtest.end_date, '%Y-%m-%d')
        st.date_input("end_date", format="YYYY-MM-DD", key="edit_opt_v7_end_date")

    # logging
    @st.fragment
    def fragment_logging(self):
        if "edit_opt_v7_logging_level" in st.session_state:
            if st.session_state.edit_opt_v7_logging_level != self.config.logging.level:
                self.config.logging.level = st.session_state.edit_opt_v7_logging_level
        else:
            st.session_state.edit_opt_v7_logging_level = self.config.logging.level
        st.selectbox("logging level", Logging.LEVEL, format_func=lambda x: Logging.LEVEL.get(x), key="edit_opt_v7_logging_level", help=pbgui_help.logging_level)

    # starting_balance
    @st.fragment
    def fragment_starting_balance(self):
        if "edit_opt_v7_starting_balance" in st.session_state:
            if st.session_state.edit_opt_v7_starting_balance != self.config.backtest.starting_balance:
                self.config.backtest.starting_balance = st.session_state.edit_opt_v7_starting_balance
        else:
            st.session_state.edit_opt_v7_starting_balance = float(self.config.backtest.starting_balance)
        st.number_input("starting_balance", step=500.0, key="edit_opt_v7_starting_balance")

    # balance_sample_divider
    @st.fragment
    def fragment_balance_sample_divider(self):
        if "edit_opt_v7_balance_sample_divider" in st.session_state:
            if st.session_state.edit_opt_v7_balance_sample_divider != self.config.backtest.balance_sample_divider:
                self.config.backtest.balance_sample_divider = st.session_state.edit_opt_v7_balance_sample_divider
        else:
            st.session_state.edit_opt_v7_balance_sample_divider = int(self.config.backtest.balance_sample_divider)
        st.number_input("balance_sample_divider", min_value=1, step=1, format="%d", key="edit_opt_v7_balance_sample_divider", help=pbgui_help.balance_sample_divider)

    # btc_collateral_cap
    @st.fragment
    def fragment_btc_collateral_cap(self):
        if "edit_opt_v7_btc_collateral_cap" in st.session_state:
            if st.session_state.edit_opt_v7_btc_collateral_cap != self.config.backtest.btc_collateral_cap:
                self.config.backtest.btc_collateral_cap = st.session_state.edit_opt_v7_btc_collateral_cap
        else:
            st.session_state.edit_opt_v7_btc_collateral_cap = float(self.config.backtest.btc_collateral_cap)
        st.number_input("btc_collateral_cap", min_value=0.0, max_value=1.0, step=0.01, format="%.4f", key="edit_opt_v7_btc_collateral_cap", help=pbgui_help.btc_collateral_cap)

    # btc_collateral_ltv_cap
    @st.fragment
    def fragment_btc_collateral_ltv_cap(self):
        current = "" if self.config.backtest.btc_collateral_ltv_cap is None else str(self.config.backtest.btc_collateral_ltv_cap)
        if "edit_opt_v7_btc_collateral_ltv_cap" not in st.session_state:
            st.session_state.edit_opt_v7_btc_collateral_ltv_cap = current
        if st.session_state.edit_opt_v7_btc_collateral_ltv_cap != current:
            text_value = st.session_state.edit_opt_v7_btc_collateral_ltv_cap.strip()
            if text_value == "":
                self.config.backtest.btc_collateral_ltv_cap = None
            else:
                try:
                    self.config.backtest.btc_collateral_ltv_cap = float(text_value)
                except Exception:
                    error_popup("Invalid btc_collateral_ltv_cap; leave empty or enter a number.")
                    st.session_state.edit_opt_v7_btc_collateral_ltv_cap = current
        st.text_input("btc_collateral_ltv_cap (empty for None)", key="edit_opt_v7_btc_collateral_ltv_cap", help=pbgui_help.btc_collateral_ltv_cap)

    # filter_by_min_effective_cost
    @st.fragment
    def fragment_filter_by_min_effective_cost_bt(self):
        options = {
            "auto (passivbot default)": None,
            "true": True,
            "false": False
        }
        current_value = self.config.backtest.filter_by_min_effective_cost
        if "edit_opt_v7_filter_by_min_effective_cost" not in st.session_state:
            st.session_state.edit_opt_v7_filter_by_min_effective_cost = next(
                (label for label, value in options.items() if value == current_value),
                "auto (passivbot default)"
            )
        selection = st.selectbox("filter_by_min_effective_cost", options.keys(), key="edit_opt_v7_filter_by_min_effective_cost", help=pbgui_help.filter_by_min_effective_cost_backtest)
        self.config.backtest.filter_by_min_effective_cost = options.get(selection)

    # iters
    @st.fragment
    def fragment_iters(self):
        # clamp to enforce 150k-300k as requested
        min_iters = 150000
        max_iters = 300000
        if "edit_opt_v7_iters" in st.session_state:
            if st.session_state.edit_opt_v7_iters != self.config.optimize.iters:
                self.config.optimize.iters = max(min_iters, min(max_iters, st.session_state.edit_opt_v7_iters))
        else:
            self.config.optimize.iters = max(min_iters, min(max_iters, self.config.optimize.iters))
            st.session_state.edit_opt_v7_iters = self.config.optimize.iters
        st.number_input("iters", min_value=min_iters, max_value=max_iters, step=1000, key="edit_opt_v7_iters", help=pbgui_help.opt_iters)

    # n_cpus
    @st.fragment
    def fragment_n_cpus(self):
        if "edit_opt_v7_n_cpus" in st.session_state:
            if st.session_state.edit_opt_v7_n_cpus != self.config.optimize.n_cpus:
                self.config.optimize.n_cpus = st.session_state.edit_opt_v7_n_cpus
        else:
            st.session_state.edit_opt_v7_n_cpus = self.config.optimize.n_cpus
        st.number_input("n_cpus", min_value=1, max_value=multiprocessing.cpu_count(), step=1, key="edit_opt_v7_n_cpus")

    # starting_config
    @st.fragment
    def fragment_starting_config(self):
        if "edit_opt_v7_starting_config" in st.session_state:
            if st.session_state.edit_opt_v7_starting_config != self.config.pbgui.starting_config:
                self.config.pbgui.starting_config = st.session_state.edit_opt_v7_starting_config
        else:
            st.session_state.edit_opt_v7_starting_config = self.config.pbgui.starting_config
        st.checkbox("starting_config", key="edit_opt_v7_starting_config", help=pbgui_help.starting_config)

    # combine_ohlcvs
    @st.fragment
    def fragment_combine_ohlcvs(self):
        if "edit_opt_v7_combine_ohlcvs" in st.session_state:
            if st.session_state.edit_opt_v7_combine_ohlcvs != self.config.backtest.combine_ohlcvs:
                self.config.backtest.combine_ohlcvs = st.session_state.edit_opt_v7_combine_ohlcvs
        else:
            st.session_state.edit_opt_v7_combine_ohlcvs = self.config.backtest.combine_ohlcvs
        st.checkbox("combine_ohlcvs", key="edit_opt_v7_combine_ohlcvs", help=pbgui_help.combine_ohlcvs)

    # compress_results_file
    @st.fragment
    def fragment_compress_results_file(self):
        if "edit_opt_v7_compress_results_file" in st.session_state:
            if st.session_state.edit_opt_v7_compress_results_file != self.config.optimize.compress_results_file:
                self.config.optimize.compress_results_file = st.session_state.edit_opt_v7_compress_results_file
        else:
            st.session_state.edit_opt_v7_compress_results_file = self.config.optimize.compress_results_file
        st.checkbox("compress_results_file", key="edit_opt_v7_compress_results_file", help=pbgui_help.compress_results_file)

    # write_all_results
    @st.fragment
    def fragment_write_all_results(self):
        if "edit_opt_v7_write_all_results" in st.session_state:
            if st.session_state.edit_opt_v7_write_all_results != self.config.optimize.write_all_results:
                self.config.optimize.write_all_results = st.session_state.edit_opt_v7_write_all_results
        else:
            st.session_state.edit_opt_v7_write_all_results = self.config.optimize.write_all_results
        st.checkbox("write_all_results", key="edit_opt_v7_write_all_results", help=pbgui_help.write_all_results)

    # population_size
    @st.fragment
    def fragment_population_size(self):
        if "edit_opt_v7_population_size" in st.session_state:
            if st.session_state.edit_opt_v7_population_size != self.config.optimize.population_size:
                self.config.optimize.population_size = st.session_state.edit_opt_v7_population_size
        else:
            st.session_state.edit_opt_v7_population_size = self.config.optimize.population_size
        st.number_input("population_size", min_value=1, max_value=10000, step=1, format="%d", key="edit_opt_v7_population_size", help=pbgui_help.population_size)

    # offspring_multiplier
    @st.fragment
    def fragment_offspring_multiplier(self):
        if "edit_opt_v7_offspring_multiplier" in st.session_state:
            if st.session_state.edit_opt_v7_offspring_multiplier != self.config.optimize.offspring_multiplier:
                self.config.optimize.offspring_multiplier = st.session_state.edit_opt_v7_offspring_multiplier
        else:
            st.session_state.edit_opt_v7_offspring_multiplier = self.config.optimize.offspring_multiplier
        st.number_input("offspring_multiplier", min_value=0.0, max_value=10000.0, step=0.1, format="%.2f", key="edit_opt_v7_offspring_multiplier", help=pbgui_help.offspring_multiplier)

    # crossover_probability
    @st.fragment
    def fragment_crossover_probability(self):
        if "edit_opt_v7_crossover_probability" in st.session_state:
            if st.session_state.edit_opt_v7_crossover_probability != self.config.optimize.crossover_probability:
                self.config.optimize.crossover_probability = st.session_state.edit_opt_v7_crossover_probability
        else:
            st.session_state.edit_opt_v7_crossover_probability = self.config.optimize.crossover_probability
        st.number_input("crossover_probability", min_value=0.0, max_value=1.0, step=0.01, format="%.2f", key="edit_opt_v7_crossover_probability", help=pbgui_help.crossover_probability)
    
    # crossover_eta
    @st.fragment
    def fragment_crossover_eta(self):
        if "edit_opt_v7_crossover_eta" in st.session_state:
            if st.session_state.edit_opt_v7_crossover_eta != self.config.optimize.crossover_eta:
                self.config.optimize.crossover_eta = st.session_state.edit_opt_v7_crossover_eta
        else:
            st.session_state.edit_opt_v7_crossover_eta = self.config.optimize.crossover_eta
        st.number_input("crossover_eta", min_value=0.0, max_value=10000.0, step=1.0, format="%.2f", key="edit_opt_v7_crossover_eta", help=pbgui_help.crossover_eta)
    
    # mutation_probability
    @st.fragment
    def fragment_mutation_probability(self):
        if "edit_opt_v7_mutation_probability" in st.session_state:
            if st.session_state.edit_opt_v7_mutation_probability != self.config.optimize.mutation_probability:
                self.config.optimize.mutation_probability = st.session_state.edit_opt_v7_mutation_probability
        else:
            st.session_state.edit_opt_v7_mutation_probability = self.config.optimize.mutation_probability
        st.number_input("mutation_probability", min_value=0.0, max_value=1.0, step=0.01, format="%.2f", key="edit_opt_v7_mutation_probability", help=pbgui_help.mutation_probability)

    # mutation_eta
    @st.fragment
    def fragment_mutation_eta(self):
        if "edit_opt_v7_mutation_eta" in st.session_state:
            if st.session_state.edit_opt_v7_mutation_eta != self.config.optimize.mutation_eta:
                self.config.optimize.mutation_eta = st.session_state.edit_opt_v7_mutation_eta
        else:
            st.session_state.edit_opt_v7_mutation_eta = self.config.optimize.mutation_eta
        st.number_input("mutation_eta", min_value=0.0, max_value=10000.0, step=1.0, format="%.2f", key="edit_opt_v7_mutation_eta", help=pbgui_help.mutation_eta)

    # mutation_indpb
    @st.fragment
    def fragment_mutation_indpb(self):
        if "edit_opt_v7_mutation_indpb" in st.session_state:
            if st.session_state.edit_opt_v7_mutation_indpb != self.config.optimize.mutation_indpb:
                self.config.optimize.mutation_indpb = st.session_state.edit_opt_v7_mutation_indpb
        else:
            st.session_state.edit_opt_v7_mutation_indpb = self.config.optimize.mutation_indpb
        st.number_input("mutation_indpb", min_value=0.0, max_value=1.0, step=0.01, format="%.2f", key="edit_opt_v7_mutation_indpb", help=pbgui_help.mutation_indpb)
    
    # scoring
    @st.fragment
    def fragment_scoring(self):
        if "edit_opt_v7_scoring" in st.session_state:
            if st.session_state.edit_opt_v7_scoring != self.config.optimize.scoring:
                self.config.optimize.scoring = st.session_state.edit_opt_v7_scoring
        else:
            st.session_state.edit_opt_v7_scoring = self.config.optimize.scoring
        st.multiselect(
            "scoring", 
            [
            "adg",
            "adg_per_exposure_long",
            "adg_per_exposure_short",
            "adg_w",
            "adg_w_per_exposure_long",
            "adg_w_per_exposure_short",
            "btc_adg",
            "btc_adg_per_exposure_long",
            "btc_adg_per_exposure_short",
            "btc_adg_w",
            "btc_adg_w_per_exposure_long",
            "btc_adg_w_per_exposure_short",
            "btc_calmar_ratio",
            "btc_calmar_ratio_w",
            "btc_drawdown_worst",
            "btc_drawdown_worst_mean_1pct",
            "btc_equity_balance_diff_neg_max",
            "btc_equity_balance_diff_neg_mean",
            "btc_equity_balance_diff_pos_max",
            "btc_equity_balance_diff_pos_mean",
            "btc_equity_choppiness",
            "btc_equity_choppiness_w",
            "btc_equity_jerkiness",
            "btc_equity_jerkiness_w",
            "btc_expected_shortfall_1pct",
            "btc_exponential_fit_error",
            "btc_exponential_fit_error_w",
            "btc_gain",
            "btc_gain_per_exposure_long",
            "btc_gain_per_exposure_short",
            "btc_loss_profit_ratio",
            "btc_loss_profit_ratio_w",
            "btc_mdg",
            "btc_mdg_per_exposure_long",
            "btc_mdg_per_exposure_short",
            "btc_mdg_w",
            "btc_mdg_w_per_exposure_long",
            "btc_mdg_w_per_exposure_short",
            "btc_omega_ratio",
            "btc_omega_ratio_w",
            "btc_sharpe_ratio",
            "btc_sharpe_ratio_w",
            "btc_sortino_ratio",
            "btc_sortino_ratio_w",
            "btc_sterling_ratio",
            "btc_sterling_ratio_w",
            "calmar_ratio",
            "calmar_ratio_w",
            "drawdown_worst",
            "drawdown_worst_mean_1pct",
            "equity_balance_diff_neg_max",
            "equity_balance_diff_neg_mean",
            "equity_balance_diff_pos_max",
            "equity_balance_diff_pos_mean",
            "equity_choppiness",
            "equity_choppiness_w",
            "equity_jerkiness",
            "equity_jerkiness_w",
            "expected_shortfall_1pct",
            "exponential_fit_error",
            "exponential_fit_error_w",
            "flat_btc_balance_hours",
            "gain",
            "gain_per_exposure_long",
            "gain_per_exposure_short",
            "loss_profit_ratio",
            "loss_profit_ratio_w",
            "mdg",
            "mdg_per_exposure_long",
            "mdg_per_exposure_short",
            "mdg_w",
            "mdg_w_per_exposure_long",
            "mdg_w_per_exposure_short",
            "omega_ratio",
            "omega_ratio_w",
            "position_held_hours_max",
            "position_held_hours_mean",
            "position_held_hours_median",
            "position_unchanged_hours_max",
            "positions_held_per_day",
            "sharpe_ratio",
            "sharpe_ratio_w",
            "sortino_ratio",
            "sortino_ratio_w",
            "sterling_ratio",
            "sterling_ratio_w",
            "volume_pct_per_day_avg",
            "volume_pct_per_day_avg_w",
            ], 
            key="edit_opt_v7_scoring",
            help=pbgui_help.scoring,
        )

    def _selected_symbols_for_guide(self) -> list[str]:
        symbols = []
        for key in ("edit_opt_v7_approved_coins_long", "edit_opt_v7_approved_coins_short"):
            if key in st.session_state:
                symbols.extend(st.session_state[key])
        if not symbols:
            symbols.extend(self.config.live.approved_coins.long)
            symbols.extend(self.config.live.approved_coins.short)
        # Preserve order while deduping
        return list(dict.fromkeys(symbols))

    def _lookup_symbol_data(self, symbol: str | None):
        if not symbol:
            return None
        for key in ("coindata_bybit", "coindata_binance", "coindata_gateio", "coindata_bitget"):
            cd = st.session_state.get(key)
            if not cd:
                continue
            try:
                for info in cd.symbols_data:
                    if info.get("symbol") == symbol:
                        return info
            except Exception:
                continue
        return None

    def _format_market_cap(self, cap: int | float | None) -> str:
        if not cap or cap <= 0:
            return "n/a"
        for unit, div in (("T", 1e12), ("B", 1e9), ("M", 1e6)):
            if cap >= div:
                return f'{cap/div:.1f}{unit}'
        return f'{cap:,.0f}'

    def _build_profile(self) -> dict:
        symbols = self._selected_symbols_for_guide()
        primary = symbols[0] if symbols else None
        info = self._lookup_symbol_data(primary)
        cap = info.get("market_cap") if info else 0
        vol_mcap = info.get("vol/mcap") if info else None
        vol24 = info.get("volume_24h") if info else None
        tags = info.get("tags") if info else []
        price = info.get("price") if info else None
        is_cpt = info.get("copy_trading") if info else False
        hype = bool(vol_mcap and vol_mcap >= 0.35)
        memecoin = any(t.lower() in {"meme", "memecoin", "doge", "shib"} for t in tags) or (primary and primary.lower().startswith(("pepe", "bonk", "doge", "shib", "floki")))
        if primary and primary.startswith("BTC"):
            segment = "btc_like"
        elif cap and cap >= 10_000_000_000:
            segment = "large_cap"
        elif cap and cap >= 1_000_000_000:
            segment = "mid_cap"
        elif cap and cap >= 200_000_000:
            segment = "small_cap"
        else:
            segment = "micro_cap"
        label = {
            "btc_like": "Deep-liquidity large cap",
            "large_cap": "Large cap",
            "mid_cap": "Mid cap",
            "small_cap": "Small cap",
            "micro_cap": "Micro/high beta",
        }.get(segment, "Mixed basket")
        return {
            "primary": primary,
            "segment": segment,
            "label": label,
            "cap": cap or 0,
            "vol_mcap": vol_mcap,
            "hype": hype,
            "vol24": vol24,
            "tags": tags,
            "price": price,
            "copy_trading": is_cpt,
            "memecoin": memecoin,
        }

    def _genetics_recommendation(self, profile: dict) -> str:
        opt = self.config.optimize
        segment = profile["segment"]
        hype = profile["hype"]
        if segment in ("btc_like", "large_cap"):
            cp_range = (0.55, 0.70)
            mp_range = (0.18, 0.32)
            indpb_range = (0.05, 0.12)
            eta_range = (18, 28)
        elif segment in ("mid_cap", "small_cap") and not hype:
            cp_range = (0.6, 0.78)
            mp_range = (0.25, 0.45)
            indpb_range = (0.1, 0.22)
            eta_range = (14, 24)
        else:
            cp_range = (0.65, 0.85)
            mp_range = (0.35, 0.55)
            indpb_range = (0.18, 0.32)
            eta_range = (10, 18)
        return (
            f"Crossover {cp_range[0]:.2f}-{cp_range[1]:.2f} "
            f"(current {opt.crossover_probability:.2f}), mutation {mp_range[0]:.2f}-{mp_range[1]:.2f} "
            f"(current {opt.mutation_probability:.2f}, indpb {opt.mutation_indpb:.2f}), "
            f"eta {eta_range[0]}-{eta_range[1]} (current {opt.mutation_eta:.1f})."
        )

    def _long_bounds_profiles(self, profile: dict) -> dict:
        """Return recommended ranges for long bounds keyed by profile (risky/conservative)."""
        # volatility_factor based on vol/mcap, hype, memecoin tags
        vol_factor = 1.0
        if profile["vol_mcap"] and profile["vol_mcap"] > 0:
            vol_factor += min(max((profile["vol_mcap"] - 0.15) * 1.2, -0.25), 0.75)
        if profile["hype"] or profile["memecoin"]:
            vol_factor += 0.35
        if profile["segment"] in ("btc_like", "large_cap"):
            vol_factor -= 0.2
        vol_factor = max(0.6, min(1.6, vol_factor))

        def scale(base):
            return (round(base[0] * vol_factor, 4), round(base[1] * vol_factor, 4))

        base = {
            "long_close_grid_markup_start": (0.01, 0.06),
            "long_close_grid_markup_end": (0.015, 0.08),
            "long_close_grid_qty_pct": (0.05, 0.4),
            "long_close_trailing_grid_ratio": (-0.2, 0.2),
            "long_close_trailing_qty_pct": (0.05, 0.35),
            "long_close_trailing_retracement_pct": (0.01, 0.12),
            "long_close_trailing_threshold_pct": (-0.05, 0.2),
            "long_ema_span_0": (50, 500),
            "long_ema_span_1": (100, 800),
            "long_entry_grid_double_down_factor": (0.6, 2.0),
            "long_entry_grid_spacing_log_span_hours": (12, 72),
            "long_entry_grid_spacing_log_weight": (0.4, 1.6),
            "long_entry_grid_spacing_pct": (0.01, 0.08),
            "long_entry_grid_spacing_we_weight": (0.8, 1.4),
            "long_entry_initial_ema_dist": (-0.05, 0.03),
            "long_entry_initial_qty_pct": (0.08, 0.35),
            "long_entry_trailing_double_down_factor": (0.8, 2.5),
            "long_entry_trailing_grid_ratio": (-0.15, 0.25),
            "long_entry_trailing_retracement_pct": (0.02, 0.14),
            "long_entry_trailing_threshold_pct": (-0.08, 0.15),
            "long_filter_log_range_ema_span": (24, 240),
            "long_filter_volume_drop_pct": (0.05, 0.3),
            "long_filter_volume_ema_span": (6, 48),
            "long_n_positions": (2, 8),
            "long_total_wallet_exposure_limit": (0.18, 0.55),
            "long_unstuck_close_pct": (0.02, 0.2),
            "long_unstuck_ema_dist": (-0.1, 0.05),
            "long_unstuck_loss_allowance_pct": (0.1, 0.45),
            "long_unstuck_threshold": (-0.08, 0.08),
        }
        risky = {k: scale((v[0]*0.8, v[1]*1.2)) for k, v in base.items()}
        conservative = {k: scale((v[0]*1.0, v[1]*0.9)) for k, v in base.items()}
        return {"risky": risky, "conservative": conservative}

    def _limits_recommendation(self, profile: dict) -> str:
        limits = self.config.optimize.limits or {}
        hints = []
        if not any("drawdown_worst" in k for k in limits):
            hints.append("add drawdown_worst cap 0.30-0.45 to stop equity holes")
        if not any("drawdown_worst_mean_1pct" in k for k in limits):
            hints.append("add drawdown_worst_mean_1pct 0.20-0.30 for smoother equity")
        if not any("equity_choppiness" in k for k in limits):
            hints.append("penalize equity_choppiness_w above 0.35-0.45 to avoid staircasing")
        if not any("equity_jerkiness" in k for k in limits):
            hints.append("cap equity_jerkiness_w under 0.30-0.40 to reduce whipsaw risk")
        if profile["segment"] not in ("btc_like", "large_cap") and not any("volume_pct_per_day_avg" in k for k in limits):
            hints.append("limit volume_pct_per_day_avg under 8-12 to avoid illiquid fills")
        if not any("exponential_fit_error" in k for k in limits):
            hints.append("add exponential_fit_error_w under 0.25 to avoid runaway drift")
        if not any("positions_held_per_day" in k for k in limits):
            hints.append("penalize positions_held_per_day above 18-30 to avoid over-trading")
        if not hints:
            hints.append("limits set looks good; tighten choppiness/jerkiness if equity staircases")
        return "; ".join(hints) + "."

    def _runtime_recommendation(self, profile: dict) -> str:
        opt = self.config.optimize
        if profile["segment"] in ("btc_like", "large_cap"):
            iters = "150k-200k"
            pop = "600-900"
            offspring = "1.0-1.2"
        elif profile["segment"] in ("mid_cap", "small_cap") and not profile["hype"]:
            iters = "180k-240k"
            pop = "900-1300"
            offspring = "1.1-1.3"
        else:
            iters = "220k-300k"
            pop = "1200-1700"
            offspring = "1.2-1.4"
        return (
            f"Iters {iters} (current {opt.iters:,}), population {pop} "
            f"(current {opt.population_size:,}), offspring_multiplier {offspring} "
            f"(current {opt.offspring_multiplier:.2f})."
        )

    def _historical_samples(self, symbol: str | None, max_files: int = 30):
        """Collect prior configs for the symbol from presets, queue, and backtests to learn from past runs."""
        files = []
        pbgdir = PBGDIR
        if symbol:
            files.extend(sorted(Path(f"{pbgdir}/data/opt_v7_presets").glob("*.json")))
            files.extend(sorted(Path(f"{pbgdir}/data/opt_v7_queue").glob("*.json")))
            files.extend(sorted(Path(f"{pb7dir()}/backtests/pbgui").glob("**/config.json")))
        samples = []
        for path in files[:max_files]:
            try:
                with open(path, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                approved = (cfg.get("live", {}) or {}).get("approved_coins", {}).get("long", [])
                if symbol and approved and symbol not in approved:
                    continue
                samples.append(cfg)
            except Exception:
                continue
        return samples

    def _historical_bounds_hint(self, symbol: str | None) -> str:
        samples = self._historical_samples(symbol)
        if not samples:
            return "No archive/previous configs found for this coin yet."
        keys = [
            ("optimize", "bounds", "long_entry_grid_spacing_pct"),
            ("optimize", "bounds", "long_close_grid_markup_start"),
            ("optimize", "bounds", "long_total_wallet_exposure_limit"),
            ("optimize", "bounds", "long_entry_initial_qty_pct"),
            ("optimize", "bounds", "long_entry_trailing_threshold_pct"),
        ]
        agg = {}
        for cfg in samples:
            for path in keys:
                cur = cfg
                for p in path:
                    cur = cur.get(p, {})
                    if cur is None:
                        break
                if isinstance(cur, list) and len(cur) == 2:
                    agg.setdefault(path[-1], []).append(cur)
        parts = []
        for k, vals in agg.items():
            lows = [v[0] for v in vals]
            highs = [v[1] for v in vals]
            parts.append(f"{k}: ~{statistics.median(lows):.3f}-{statistics.median(highs):.3f} from {len(vals)} runs")
        if not parts:
            return "Archive configs loaded but no bounds found to learn from."
        return "Archive/past medians → " + "; ".join(parts)

    @st.fragment
    def fragment_optimizer_guide(self):
        try:
            profile = self._build_profile()
            symbol_line = "No coin selected; using generic guidance."
            if profile["primary"]:
                vol_text = f', vol/mcap {profile["vol_mcap"]:.2f}' if profile["vol_mcap"] else ""
                symbol_line = (
                    f'Focus: {profile["primary"]} ({profile["label"]}, '
                    f'mcap {self._format_market_cap(profile["cap"])}{vol_text})'
                )
            risk_pref = st.session_state.get("opt_v7_risk_pref", "conservative")
            risk_pref = st.radio("Risk preference", ["conservative", "risky"], horizontal=True, key="opt_v7_risk_pref")
            bounds_profiles = self._long_bounds_profiles(profile)
            chosen_bounds = bounds_profiles.get(risk_pref, bounds_profiles["conservative"])
            hist_hint = self._historical_bounds_hint(profile["primary"])
            with st.container(border=True):
                st.subheader("Optimizer Guide")
                st.caption(symbol_line)
                g1, g2 = st.columns([1,1])
                with g1:
                    st.markdown(f"- **Genetics:** {self._genetics_recommendation(profile)}")
                    st.markdown(f"- **Runtime:** {self._runtime_recommendation(profile)}")
                    st.markdown(f"- **Coins:** {self._coin_recommendation(profile)}")
                    st.markdown(f"- **History:** {hist_hint}")
                with g2:
                    st.markdown(f"- **Limits:** {self._limits_recommendation(profile)}")
                    st.markdown(f"- **Scoring:** {self._scoring_recommendation(profile)}")
                st.markdown("**Long bounds (tailored)**")
                colA, colB = st.columns([1,1])
                with colA:
                    st.markdown(
                        "\n".join([
                            f"- close markup start {chosen_bounds['long_close_grid_markup_start'][0]:.3f}-{chosen_bounds['long_close_grid_markup_start'][1]:.3f}",
                            f"- close markup end {chosen_bounds['long_close_grid_markup_end'][0]:.3f}-{chosen_bounds['long_close_grid_markup_end'][1]:.3f}",
                            f"- close qty pct {chosen_bounds['long_close_grid_qty_pct'][0]:.2f}-{chosen_bounds['long_close_grid_qty_pct'][1]:.2f}",
                            f"- trailing close retrace {chosen_bounds['long_close_trailing_retracement_pct'][0]:.3f}-{chosen_bounds['long_close_trailing_retracement_pct'][1]:.3f}",
                            f"- trailing close threshold {chosen_bounds['long_close_trailing_threshold_pct'][0]:.3f}-{chosen_bounds['long_close_trailing_threshold_pct'][1]:.3f}",
                            f"- EMA spans {chosen_bounds['long_ema_span_0'][0]:.0f}-{chosen_bounds['long_ema_span_0'][1]:.0f}/{chosen_bounds['long_ema_span_1'][0]:.0f}-{chosen_bounds['long_ema_span_1'][1]:.0f}",
                            f"- entry spacing pct {chosen_bounds['long_entry_grid_spacing_pct'][0]:.3f}-{chosen_bounds['long_entry_grid_spacing_pct'][1]:.3f}",
                            f"- entry spacing log span {chosen_bounds['long_entry_grid_spacing_log_span_hours'][0]:.0f}-{chosen_bounds['long_entry_grid_spacing_log_span_hours'][1]:.0f}h",
                            f"- entry spacing log weight {chosen_bounds['long_entry_grid_spacing_log_weight'][0]:.2f}-{chosen_bounds['long_entry_grid_spacing_log_weight'][1]:.2f}",
                            f"- entry initial qty pct {chosen_bounds['long_entry_initial_qty_pct'][0]:.2f}-{chosen_bounds['long_entry_initial_qty_pct'][1]:.2f}",
                            f"- entry trailing threshold {chosen_bounds['long_entry_trailing_threshold_pct'][0]:.3f}-{chosen_bounds['long_entry_trailing_threshold_pct'][1]:.3f}",
                            f"- entry trailing retrace {chosen_bounds['long_entry_trailing_retracement_pct'][0]:.3f}-{chosen_bounds['long_entry_trailing_retracement_pct'][1]:.3f}",
                            f"- entry trailing DD factor {chosen_bounds['long_entry_trailing_double_down_factor'][0]:.2f}-{chosen_bounds['long_entry_trailing_double_down_factor'][1]:.2f}",
                            f"- entry DD factor {chosen_bounds['long_entry_grid_double_down_factor'][0]:.2f}-{chosen_bounds['long_entry_grid_double_down_factor'][1]:.2f}",
                        ])
                    )
                with colB:
                    st.markdown(
                        "\n".join([
                            f"- filter log range ema span {chosen_bounds['long_filter_log_range_ema_span'][0]:.0f}-{chosen_bounds['long_filter_log_range_ema_span'][1]:.0f}",
                            f"- filter vol drop pct {chosen_bounds['long_filter_volume_drop_pct'][0]:.2f}-{chosen_bounds['long_filter_volume_drop_pct'][1]:.2f}",
                            f"- filter vol ema span {chosen_bounds['long_filter_volume_ema_span'][0]:.0f}-{chosen_bounds['long_filter_volume_ema_span'][1]:.0f}",
                            f"- total_wallet_exposure_limit {chosen_bounds['long_total_wallet_exposure_limit'][0]:.2f}-{chosen_bounds['long_total_wallet_exposure_limit'][1]:.2f}",
                            f"- n_positions {int(chosen_bounds['long_n_positions'][0])}-{int(chosen_bounds['long_n_positions'][1])}",
                            f"- unstuck close pct {chosen_bounds['long_unstuck_close_pct'][0]:.2f}-{chosen_bounds['long_unstuck_close_pct'][1]:.2f}",
                            f"- unstuck ema dist {chosen_bounds['long_unstuck_ema_dist'][0]:.3f}-{chosen_bounds['long_unstuck_ema_dist'][1]:.3f}",
                            f"- unstuck loss allowance {chosen_bounds['long_unstuck_loss_allowance_pct'][0]:.2f}-{chosen_bounds['long_unstuck_loss_allowance_pct'][1]:.2f}",
                            f"- unstuck threshold {chosen_bounds['long_unstuck_threshold'][0]:.3f}-{chosen_bounds['long_unstuck_threshold'][1]:.3f}",
                            f"- close trailing qty pct {chosen_bounds['long_close_trailing_qty_pct'][0]:.2f}-{chosen_bounds['long_close_trailing_qty_pct'][1]:.2f}",
                            f"- close trailing grid ratio {chosen_bounds['long_close_trailing_grid_ratio'][0]:.2f}-{chosen_bounds['long_close_trailing_grid_ratio'][1]:.2f}",
                        ])
                    )
        except Exception as e:
            st.warning(f"Optimizer guide unavailable: {e}")

    def _coin_recommendation(self, profile: dict) -> str:
        # Encourage using filters and highlight thin liquidity
        apply_filters = st.session_state.get("edit_opt_v7_apply_filters", False)
        vol_mcap = profile.get("vol_mcap")
        if profile["primary"] and vol_mcap and vol_mcap > 0.5 and not apply_filters:
            return "high vol/mcap; enable apply_filters and raise market_cap floor to avoid pumpy pairs."
        if not profile["primary"]:
            return "pick 1-3 approved coins or turn on apply_filters to auto-select liquid names."
        if profile["segment"] in ("micro_cap",) and not apply_filters:
            return "micro-cap selected; consider apply_filters + vol/mcap < 0.30 to avoid slippage."
        return "basket looks fine; widen approved list to 3-5 coins for more stable optimizer fitness."

    def _scoring_recommendation(self, profile: dict) -> str:
        scoring = set(self.config.optimize.scoring or [])
        suggestions = []
        if not scoring.intersection({"gain", "adg", "mdg", "btc_gain"}):
            suggestions.append("add gain/mdg to keep profitability pressure")
        if not scoring.intersection({"drawdown_worst", "btc_drawdown_worst"}):
            suggestions.append("add drawdown_worst to avoid sharp holes")
        if profile["segment"] in ("micro_cap",) and not scoring.intersection({"equity_jerkiness", "equity_choppiness"}):
            suggestions.append("track equity_jerkiness on high beta names")
        if profile["segment"] in ("btc_like", "large_cap") and not scoring.intersection({"sharpe_ratio", "omega_ratio"}):
            suggestions.append("blend sharpe_ratio/omega_ratio for smoothness")
        if not suggestions:
            suggestions.append("current scoring looks balanced; consider weighting btc_* metrics if collateralized in BTC")
        current = ", ".join(sorted(scoring)) if scoring else "none"
        return f"{'; '.join(suggestions)} (current: {current})."

    # filters
    def fragment_filter_coins(self):
        col1, col2, col3, col4, col5 = st.columns([1,1,1,0.5,0.5], vertical_alignment="bottom")
        with col1:
            self.fragment_market_cap()
        with col2:
            self.fragment_vol_mcap()
        with col3:
            self.fragment_tags()
        with col4:
            self.fragment_only_cpt()
        with col5:
            st.checkbox("apply_filters", value=False, help=pbgui_help.apply_filters, key="edit_opt_v7_apply_filters")
        # Init session state for approved_coins
        if "edit_opt_v7_approved_coins_long" in st.session_state:
            if st.session_state.edit_opt_v7_approved_coins_long != self.config.live.approved_coins.long:
                self.config.live.approved_coins.long = st.session_state.edit_opt_v7_approved_coins_long
        else:
            st.session_state.edit_opt_v7_approved_coins_long = self.config.live.approved_coins.long
        if "edit_opt_v7_approved_coins_short" in st.session_state:
            if st.session_state.edit_opt_v7_approved_coins_short != self.config.live.approved_coins.short:
                self.config.live.approved_coins.short = st.session_state.edit_opt_v7_approved_coins_short
        else:
            st.session_state.edit_opt_v7_approved_coins_short = self.config.live.approved_coins.short
        # Apply filters
        if st.session_state.edit_opt_v7_apply_filters:
            self.config.live.approved_coins.long = list(set(st.session_state.coindata_bybit.approved_coins + st.session_state.coindata_binance.approved_coins + st.session_state.coindata_gateio.approved_coins + st.session_state.coindata_bitget.approved_coins))
            self.config.live.approved_coins.short = list(set(st.session_state.coindata_bybit.approved_coins + st.session_state.coindata_binance.approved_coins + st.session_state.coindata_gateio.approved_coins + st.session_state.coindata_bitget.approved_coins))
        # Remove unavailable symbols
        symbols = []
        if "bybit" in self.config.backtest.exchanges:
            symbols.extend(st.session_state.coindata_bybit.symbols)
        if "binance" in self.config.backtest.exchanges:
            symbols.extend(st.session_state.coindata_binance.symbols)
        if "gateio" in self.config.backtest.exchanges:
            symbols.extend(st.session_state.coindata_gateio.symbols)
        if "bitget" in self.config.backtest.exchanges:
            symbols.extend(st.session_state.coindata_bitget.symbols)
        symbols = list(set(symbols))
        # sort symbols
        symbols.sort()
        for symbol in self.config.live.approved_coins.long.copy():
            if symbol not in symbols:
                self.config.live.approved_coins.long.remove(symbol)
        for symbol in self.config.live.approved_coins.short.copy():
            if symbol not in symbols:
                self.config.live.approved_coins.short.remove(symbol)
        # Correct Display of Symbols
        if "edit_opt_v7_approved_coins_long" in st.session_state:
            st.session_state.edit_opt_v7_approved_coins_long = self.config.live.approved_coins.long
        if "edit_opt_v7_approved_coins_short" in st.session_state:
            st.session_state.edit_opt_v7_approved_coins_short = self.config.live.approved_coins.short
        # Select approved coins
        col1, col2 = st.columns([1,1], vertical_alignment="bottom")
        with col1:
            st.multiselect('approved_coins_long', symbols, key="edit_opt_v7_approved_coins_long")
        with col2:
            st.multiselect('approved_coins_short', symbols, key="edit_opt_v7_approved_coins_short")

    @st.fragment
    # market_cap
    def fragment_market_cap(self):
        if "edit_opt_v7_market_cap" in st.session_state:
            if st.session_state.edit_opt_v7_market_cap != self.config.pbgui.market_cap:
                self.config.pbgui.market_cap = st.session_state.edit_opt_v7_market_cap
                st.session_state.coindata_binance.market_cap = self.config.pbgui.market_cap
                st.session_state.coindata_bybit.market_cap = self.config.pbgui.market_cap
                st.session_state.coindata_gateio.market_cap = self.config.pbgui.market_cap
                st.session_state.coindata_bitget.market_cap = self.config.pbgui.market_cap
                if st.session_state.edit_opt_v7_apply_filters:
                    st.rerun()
        else:
            st.session_state.edit_opt_v7_market_cap = self.config.pbgui.market_cap
            st.session_state.coindata_binance.market_cap = self.config.pbgui.market_cap
            st.session_state.coindata_bybit.market_cap = self.config.pbgui.market_cap
            st.session_state.coindata_gateio.market_cap = self.config.pbgui.market_cap
            st.session_state.coindata_bitget.market_cap = self.config.pbgui.market_cap
        st.number_input("market_cap", min_value=0, step=50, format="%.d", key="edit_opt_v7_market_cap", help=pbgui_help.market_cap)
    
    @st.fragment
    # vol_mcap
    def fragment_vol_mcap(self):
        if "edit_opt_v7_vol_mcap" in st.session_state:
            if st.session_state.edit_opt_v7_vol_mcap != self.config.pbgui.vol_mcap:
                self.config.pbgui.vol_mcap = st.session_state.edit_opt_v7_vol_mcap
                st.session_state.coindata_bybit.vol_mcap = self.config.pbgui.vol_mcap
                st.session_state.coindata_binance.vol_mcap = self.config.pbgui.vol_mcap
                st.session_state.coindata_gateio.vol_mcap = self.config.pbgui.vol_mcap
                st.session_state.coindata_bitget.vol_mcap = self.config.pbgui.vol_mcap
                if st.session_state.edit_opt_v7_apply_filters:
                    st.rerun()
        else:
            st.session_state.edit_opt_v7_vol_mcap = round(float(self.config.pbgui.vol_mcap),2)
            st.session_state.coindata_bybit.vol_mcap = self.config.pbgui.vol_mcap
            st.session_state.coindata_binance.vol_mcap = self.config.pbgui.vol_mcap
            st.session_state.coindata_gateio.vol_mcap = self.config.pbgui.vol_mcap
            st.session_state.coindata_bitget.vol_mcap = self.config.pbgui.vol_mcap
        st.number_input("vol/mcap", min_value=0.0, step=0.05, format="%.2f", key="edit_opt_v7_vol_mcap", help=pbgui_help.vol_mcap)

    @st.fragment
    # tags
    def fragment_tags(self):
        if "edit_opt_v7_tags" in st.session_state:
            if st.session_state.edit_opt_v7_tags != self.config.pbgui.tags:
                self.config.pbgui.tags = st.session_state.edit_opt_v7_tags
                st.session_state.coindata_bybit.tags = self.config.pbgui.tags
                st.session_state.coindata_binance.tags = self.config.pbgui.tags
                st.session_state.coindata_gateio.tags = self.config.pbgui.tags
                st.session_state.coindata_bitget.tags = self.config.pbgui.tags
                if st.session_state.edit_opt_v7_apply_filters:
                    st.rerun()
        else:
            st.session_state.edit_opt_v7_tags = self.config.pbgui.tags
            st.session_state.coindata_bybit.tags = self.config.pbgui.tags
            st.session_state.coindata_binance.tags = self.config.pbgui.tags
            st.session_state.coindata_gateio.tags = self.config.pbgui.tags
            st.session_state.coindata_bitget.tags = self.config.pbgui.tags
        # remove duplicates from tags and sort them
        tags = sorted(list(set(st.session_state.coindata_bybit.all_tags + st.session_state.coindata_binance.all_tags + st.session_state.coindata_gateio.all_tags + st.session_state.coindata_bitget.all_tags)))
        st.multiselect("tags", tags, key="edit_opt_v7_tags", help=pbgui_help.coindata_tags)

    # only_cpt
    @st.fragment
    def fragment_only_cpt(self):
        if "edit_opt_v7_only_cpt" in st.session_state:
            if st.session_state.edit_opt_v7_only_cpt != self.config.pbgui.only_cpt:
                self.config.pbgui.only_cpt = st.session_state.edit_opt_v7_only_cpt
                st.session_state.coindata_bybit.only_cpt = self.config.pbgui.only_cpt
                st.session_state.coindata_binance.only_cpt = self.config.pbgui.only_cpt
                st.session_state.coindata_bitget.only_cpt = self.config.pbgui.only_cpt
                if st.session_state.edit_opt_v7_apply_filters:
                    st.rerun()
        else:
            st.session_state.edit_opt_v7_only_cpt = self.config.pbgui.only_cpt
            st.session_state.coindata_bybit.only_cpt = self.config.pbgui.only_cpt
            st.session_state.coindata_binance.only_cpt = self.config.pbgui.only_cpt
            st.session_state.coindata_bitget.only_cpt = self.config.pbgui.only_cpt
        st.checkbox("only_cpt", key="edit_opt_v7_only_cpt", help=pbgui_help.only_cpt)

    # long_close_grid_markup_end
    @st.fragment
    def fragment_long_close_grid_markup_end(self):
        if "edit_opt_v7_long_close_grid_markup_end" in st.session_state:
            if st.session_state.edit_opt_v7_long_close_grid_markup_end != (self.config.optimize.bounds.long_close_grid_markup_end_0, self.config.optimize.bounds.long_close_grid_markup_end_1):
                self.config.optimize.bounds.long_close_grid_markup_end_0 = st.session_state.edit_opt_v7_long_close_grid_markup_end[0]
                self.config.optimize.bounds.long_close_grid_markup_end_1 = st.session_state.edit_opt_v7_long_close_grid_markup_end[1]
        else:
            st.session_state.edit_opt_v7_long_close_grid_markup_end = (self.config.optimize.bounds.long_close_grid_markup_end_0, self.config.optimize.bounds.long_close_grid_markup_end_1)
        st.slider(
            "long_close_grid_markup_end",
            min_value=Bounds.CLOSE_GRID_MARKUP_END_MIN,
            max_value=Bounds.CLOSE_GRID_MARKUP_END_MAX,
            step=Bounds.CLOSE_GRID_MARKUP_END_STEP,
            format=Bounds.CLOSE_GRID_MARKUP_END_FORMAT,
            key="edit_opt_v7_long_close_grid_markup_end",
            help=pbgui_help.close_grid_parameters)  
    
    # long_close_grid_markup_start
    @st.fragment
    def fragment_long_close_grid_markup_start(self):
        if "edit_opt_v7_long_close_grid_markup_start" in st.session_state:
            if st.session_state.edit_opt_v7_long_close_grid_markup_start != (self.config.optimize.bounds.long_close_grid_markup_start_0, self.config.optimize.bounds.long_close_grid_markup_start_1):
                self.config.optimize.bounds.long_close_grid_markup_start_0 = st.session_state.edit_opt_v7_long_close_grid_markup_start[0]
                self.config.optimize.bounds.long_close_grid_markup_start_1 = st.session_state.edit_opt_v7_long_close_grid_markup_start[1]
        else:
            st.session_state.edit_opt_v7_long_close_grid_markup_start = (self.config.optimize.bounds.long_close_grid_markup_start_0, self.config.optimize.bounds.long_close_grid_markup_start_1)
        st.slider(
            "long_close_grid_markup_start",
            min_value=Bounds.CLOSE_GRID_MARKUP_START_MIN,
            max_value=Bounds.CLOSE_GRID_MARKUP_START_MAX,
            step=Bounds.CLOSE_GRID_MARKUP_START_STEP,
            format=Bounds.CLOSE_GRID_MARKUP_START_FORMAT,
            key="edit_opt_v7_long_close_grid_markup_start",
            help=pbgui_help.close_grid_parameters)

    # long_close_grid_qty_pct
    @st.fragment
    def fragment_long_close_grid_qty_pct(self):
        if "edit_opt_v7_long_close_grid_qty_pct" in st.session_state:
            if st.session_state.edit_opt_v7_long_close_grid_qty_pct != (self.config.optimize.bounds.long_close_grid_qty_pct_0, self.config.optimize.bounds.long_close_grid_qty_pct_1):
                self.config.optimize.bounds.long_close_grid_qty_pct_0 = st.session_state.edit_opt_v7_long_close_grid_qty_pct[0]
                self.config.optimize.bounds.long_close_grid_qty_pct_1 = st.session_state.edit_opt_v7_long_close_grid_qty_pct[1]
        else:
            st.session_state.edit_opt_v7_long_close_grid_qty_pct = (self.config.optimize.bounds.long_close_grid_qty_pct_0, self.config.optimize.bounds.long_close_grid_qty_pct_1)
        st.slider(
            "long_close_grid_qty_pct",
            min_value=Bounds.CLOSE_GRID_QTY_PCT_MIN,
            max_value=Bounds.CLOSE_GRID_QTY_PCT_MAX,
            step=Bounds.CLOSE_GRID_QTY_PCT_STEP,
            format=Bounds.CLOSE_GRID_QTY_PCT_FORMAT,
            key="edit_opt_v7_long_close_grid_qty_pct",
            help=pbgui_help.close_grid_parameters)

    # long_close_trailing_grid_ratio
    @st.fragment
    def fragment_long_close_trailing_grid_ratio(self):
        if "edit_opt_v7_long_close_trailing_grid_ratio" in st.session_state:
            if st.session_state.edit_opt_v7_long_close_trailing_grid_ratio != (self.config.optimize.bounds.long_close_trailing_grid_ratio_0, self.config.optimize.bounds.long_close_trailing_grid_ratio_1):
                self.config.optimize.bounds.long_close_trailing_grid_ratio_0 = st.session_state.edit_opt_v7_long_close_trailing_grid_ratio[0]
                self.config.optimize.bounds.long_close_trailing_grid_ratio_1 = st.session_state.edit_opt_v7_long_close_trailing_grid_ratio[1]
        else:
            st.session_state.edit_opt_v7_long_close_trailing_grid_ratio = (self.config.optimize.bounds.long_close_trailing_grid_ratio_0, self.config.optimize.bounds.long_close_trailing_grid_ratio_1)
        st.slider(
            "long_close_trailing_grid_ratio",
            min_value=Bounds.CLOSE_TRAILING_GRID_RATIO_MIN,
            max_value=Bounds.CLOSE_TRAILING_GRID_RATIO_MAX,
            step=Bounds.CLOSE_TRAILING_GRID_RATIO_STEP,
            format=Bounds.CLOSE_TRAILING_GRID_RATIO_FORMAT,
            key="edit_opt_v7_long_close_trailing_grid_ratio",
            help=pbgui_help.close_grid_parameters)

    # long_close_trailing_qty_pct
    @st.fragment
    def fragment_long_close_trailing_qty_pct(self):
        if "edit_opt_v7_long_close_trailing_qty_pct" in st.session_state:
            if st.session_state.edit_opt_v7_long_close_trailing_qty_pct != (self.config.optimize.bounds.long_close_trailing_qty_pct_0, self.config.optimize.bounds.long_close_trailing_qty_pct_1):
                self.config.optimize.bounds.long_close_trailing_qty_pct_0 = st.session_state.edit_opt_v7_long_close_trailing_qty_pct[0]
                self.config.optimize.bounds.long_close_trailing_qty_pct_1 = st.session_state.edit_opt_v7_long_close_trailing_qty_pct[1]
        else:
            st.session_state.edit_opt_v7_long_close_trailing_qty_pct = (self.config.optimize.bounds.long_close_trailing_qty_pct_0, self.config.optimize.bounds.long_close_trailing_qty_pct_1)
        st.slider(
            "long_close_trailing_qty_pct",
            min_value=Bounds.CLOSE_TRAILING_QTY_PCT_MIN,
            max_value=Bounds.CLOSE_TRAILING_QTY_PCT_MAX,
            step=Bounds.CLOSE_TRAILING_QTY_PCT_STEP,
            format=Bounds.CLOSE_TRAILING_QTY_PCT_FORMAT,
            key="edit_opt_v7_long_close_trailing_qty_pct",
            help=pbgui_help.close_grid_parameters)

    # long_close_trailing_retracement_pct
    @st.fragment
    def fragment_long_close_trailing_retracement_pct(self):
        if "edit_opt_v7_long_close_trailing_retracement_pct" in st.session_state:
            if st.session_state.edit_opt_v7_long_close_trailing_retracement_pct != (self.config.optimize.bounds.long_close_trailing_retracement_pct_0, self.config.optimize.bounds.long_close_trailing_retracement_pct_1):
                self.config.optimize.bounds.long_close_trailing_retracement_pct_0 = st.session_state.edit_opt_v7_long_close_trailing_retracement_pct[0]
                self.config.optimize.bounds.long_close_trailing_retracement_pct_1 = st.session_state.edit_opt_v7_long_close_trailing_retracement_pct[1]
        else:
            st.session_state.edit_opt_v7_long_close_trailing_retracement_pct = (self.config.optimize.bounds.long_close_trailing_retracement_pct_0, self.config.optimize.bounds.long_close_trailing_retracement_pct_1)
        st.slider(
            "long_close_trailing_retracement_pct",
            min_value=Bounds.CLOSE_TRAILING_RETRACEMENT_PCT_MIN,
            max_value=Bounds.CLOSE_TRAILING_RETRACEMENT_PCT_MAX,
            step=Bounds.CLOSE_TRAILING_RETRACEMENT_PCT_STEP,
            format=Bounds.CLOSE_TRAILING_RETRACEMENT_PCT_FORMAT,
            key="edit_opt_v7_long_close_trailing_retracement_pct",
            help=pbgui_help.close_grid_parameters)

    # long_close_trailing_threshold_pct
    @st.fragment
    def fragment_long_close_trailing_threshold_pct(self):
        if "edit_opt_v7_long_close_trailing_threshold_pct" in st.session_state:
            if st.session_state.edit_opt_v7_long_close_trailing_threshold_pct != (self.config.optimize.bounds.long_close_trailing_threshold_pct_0, self.config.optimize.bounds.long_close_trailing_threshold_pct_1):
                self.config.optimize.bounds.long_close_trailing_threshold_pct_0 = st.session_state.edit_opt_v7_long_close_trailing_threshold_pct[0]
                self.config.optimize.bounds.long_close_trailing_threshold_pct_1 = st.session_state.edit_opt_v7_long_close_trailing_threshold_pct[1]
        else:
            st.session_state.edit_opt_v7_long_close_trailing_threshold_pct = (self.config.optimize.bounds.long_close_trailing_threshold_pct_0, self.config.optimize.bounds.long_close_trailing_threshold_pct_1)
        st.slider(
            "long_close_trailing_threshold_pct",
            min_value=Bounds.CLOSE_TRAILING_THRESHOLD_PCT_MIN,
            max_value=Bounds.CLOSE_TRAILING_THRESHOLD_PCT_MAX,
            step=Bounds.CLOSE_TRAILING_THRESHOLD_PCT_STEP,
            format=Bounds.CLOSE_TRAILING_THRESHOLD_PCT_FORMAT,
            key="edit_opt_v7_long_close_trailing_threshold_pct",
            help=pbgui_help.close_grid_parameters)

    # long_ema_span_0
    @st.fragment
    def fragment_long_ema_span_0(self):
        if "edit_opt_v7_long_ema_span_0" in st.session_state:
            if st.session_state.edit_opt_v7_long_ema_span_0 != (self.config.optimize.bounds.long_ema_span_0_0, self.config.optimize.bounds.long_ema_span_0_1):
                self.config.optimize.bounds.long_ema_span_0_0 = st.session_state.edit_opt_v7_long_ema_span_0[0]
                self.config.optimize.bounds.long_ema_span_0_1 = st.session_state.edit_opt_v7_long_ema_span_0[1]
        else:
            st.session_state.edit_opt_v7_long_ema_span_0 = (self.config.optimize.bounds.long_ema_span_0_0, self.config.optimize.bounds.long_ema_span_0_1)
        st.slider(
            "long_ema_span_0",
            min_value=Bounds.EMA_SPAN_0_MIN,
            max_value=Bounds.EMA_SPAN_0_MAX,
            step=Bounds.EMA_SPAN_0_STEP,
            format=Bounds.EMA_SPAN_0_FORMAT,
            key="edit_opt_v7_long_ema_span_0",
            help=pbgui_help.ema_span)
    
    # long_ema_span_1
    @st.fragment
    def fragment_long_ema_span_1(self):
        if "edit_opt_v7_long_ema_span_1" in st.session_state:
            if st.session_state.edit_opt_v7_long_ema_span_1 != (self.config.optimize.bounds.long_ema_span_1_0, self.config.optimize.bounds.long_ema_span_1_1):
                self.config.optimize.bounds.long_ema_span_1_0 = st.session_state.edit_opt_v7_long_ema_span_1[0]
                self.config.optimize.bounds.long_ema_span_1_1 = st.session_state.edit_opt_v7_long_ema_span_1[1]
        else:
            st.session_state.edit_opt_v7_long_ema_span_1 = (self.config.optimize.bounds.long_ema_span_1_0, self.config.optimize.bounds.long_ema_span_1_1)
        st.slider(
            "long_ema_span_1",
            min_value=Bounds.EMA_SPAN_1_MIN,
            max_value=Bounds.EMA_SPAN_1_MAX,
            step=Bounds.EMA_SPAN_1_STEP,
            format=Bounds.EMA_SPAN_1_FORMAT,
            key="edit_opt_v7_long_ema_span_1",
            help=pbgui_help.ema_span)
    
    # long_entry_grid_double_down_factor
    @st.fragment
    def fragment_long_entry_grid_double_down_factor(self):
        if "edit_opt_v7_long_entry_grid_double_down_factor" in st.session_state:
            if st.session_state.edit_opt_v7_long_entry_grid_double_down_factor != (self.config.optimize.bounds.long_entry_grid_double_down_factor_0, self.config.optimize.bounds.long_entry_grid_double_down_factor_1):
                self.config.optimize.bounds.long_entry_grid_double_down_factor_0 = st.session_state.edit_opt_v7_long_entry_grid_double_down_factor[0]
                self.config.optimize.bounds.long_entry_grid_double_down_factor_1 = st.session_state.edit_opt_v7_long_entry_grid_double_down_factor[1]
        else:
            st.session_state.edit_opt_v7_long_entry_grid_double_down_factor = (self.config.optimize.bounds.long_entry_grid_double_down_factor_0, self.config.optimize.bounds.long_entry_grid_double_down_factor_1)
        st.slider(
            "long_entry_grid_double_down_factor",
            min_value=Bounds.ENTRY_GRID_DOUBLE_DOWN_FACTOR_MIN,
            max_value=Bounds.ENTRY_GRID_DOUBLE_DOWN_FACTOR_MAX,
            step=Bounds.ENTRY_GRID_DOUBLE_DOWN_FACTOR_STEP,
            format=Bounds.ENTRY_GRID_DOUBLE_DOWN_FACTOR_FORMAT,
            key="edit_opt_v7_long_entry_grid_double_down_factor",
            help=pbgui_help.entry_grid_double_down_factor)
    
    # long_entry_grid_spacing_log_span_hours
    @st.fragment
    def fragment_long_entry_grid_spacing_log_span_hours(self):
        if "edit_opt_v7_long_entry_grid_spacing_log_span_hours" in st.session_state:
            if st.session_state.edit_opt_v7_long_entry_grid_spacing_log_span_hours != (self.config.optimize.bounds.long_entry_grid_spacing_log_span_hours_0, self.config.optimize.bounds.long_entry_grid_spacing_log_span_hours_1):
                self.config.optimize.bounds.long_entry_grid_spacing_log_span_hours_0 = st.session_state.edit_opt_v7_long_entry_grid_spacing_log_span_hours[0]
                self.config.optimize.bounds.long_entry_grid_spacing_log_span_hours_1 = st.session_state.edit_opt_v7_long_entry_grid_spacing_log_span_hours[1]
        else:
            st.session_state.edit_opt_v7_long_entry_grid_spacing_log_span_hours = (self.config.optimize.bounds.long_entry_grid_spacing_log_span_hours_0, self.config.optimize.bounds.long_entry_grid_spacing_log_span_hours_1)
        st.slider(
            "long_entry_grid_spacing_log_span_hours",
            min_value=Bounds.ENTRY_GRID_SPACING_LOG_SPAN_HOURS_MIN,
            max_value=Bounds.ENTRY_GRID_SPACING_LOG_SPAN_HOURS_MAX,
            step=Bounds.ENTRY_GRID_SPACING_LOG_SPAN_HOURS_STEP,
            format=Bounds.ENTRY_GRID_SPACING_LOG_SPAN_HOURS_FORMAT,
            key="edit_opt_v7_long_entry_grid_spacing_log_span_hours",
            help=pbgui_help.entry_grid_spacing_log)

    # long_entry_grid_spacing_log_weight
    @st.fragment
    def fragment_long_entry_grid_spacing_log_weight(self):
        if "edit_opt_v7_long_entry_grid_spacing_log_weight" in st.session_state:
            if st.session_state.edit_opt_v7_long_entry_grid_spacing_log_weight != (self.config.optimize.bounds.long_entry_grid_spacing_log_weight_0, self.config.optimize.bounds.long_entry_grid_spacing_log_weight_1):
                self.config.optimize.bounds.long_entry_grid_spacing_log_weight_0 = st.session_state.edit_opt_v7_long_entry_grid_spacing_log_weight[0]
                self.config.optimize.bounds.long_entry_grid_spacing_log_weight_1 = st.session_state.edit_opt_v7_long_entry_grid_spacing_log_weight[1]
        else:
            st.session_state.edit_opt_v7_long_entry_grid_spacing_log_weight = (self.config.optimize.bounds.long_entry_grid_spacing_log_weight_0, self.config.optimize.bounds.long_entry_grid_spacing_log_weight_1)
        st.slider(
            "long_entry_grid_spacing_log_weight",
            min_value=Bounds.ENTRY_GRID_SPACING_LOG_WEIGHT_MIN,
            max_value=Bounds.ENTRY_GRID_SPACING_LOG_WEIGHT_MAX,
            step=Bounds.ENTRY_GRID_SPACING_LOG_WEIGHT_STEP,
            format=Bounds.ENTRY_GRID_SPACING_LOG_WEIGHT_FORMAT,
            key="edit_opt_v7_long_entry_grid_spacing_log_weight",
            help=pbgui_help.entry_grid_spacing_log)

    # long_entry_grid_spacing_pct
    @st.fragment
    def fragment_long_entry_grid_spacing_pct(self):
        if "edit_opt_v7_long_entry_grid_spacing_pct" in st.session_state:
            if st.session_state.edit_opt_v7_long_entry_grid_spacing_pct != (self.config.optimize.bounds.long_entry_grid_spacing_pct_0, self.config.optimize.bounds.long_entry_grid_spacing_pct_1):
                self.config.optimize.bounds.long_entry_grid_spacing_pct_0 = st.session_state.edit_opt_v7_long_entry_grid_spacing_pct[0]
                self.config.optimize.bounds.long_entry_grid_spacing_pct_1 = st.session_state.edit_opt_v7_long_entry_grid_spacing_pct[1]
        else:
            st.session_state.edit_opt_v7_long_entry_grid_spacing_pct = (self.config.optimize.bounds.long_entry_grid_spacing_pct_0, self.config.optimize.bounds.long_entry_grid_spacing_pct_1)
        st.slider(
            "long_entry_grid_spacing_pct",
            min_value=Bounds.ENTRY_GRID_SPACING_PCT_MIN,
            max_value=Bounds.ENTRY_GRID_SPACING_PCT_MAX,
            step=Bounds.ENTRY_GRID_SPACING_PCT_STEP,
            format=Bounds.ENTRY_GRID_SPACING_PCT_FORMAT,
            key="edit_opt_v7_long_entry_grid_spacing_pct",
            help=pbgui_help.entry_grid_spacing)
    
    # long_entry_grid_spacing_we_weight
    @st.fragment
    def fragment_long_entry_grid_spacing_we_weight(self):
        if "edit_opt_v7_long_entry_grid_spacing_we_weight" in st.session_state:
            if st.session_state.edit_opt_v7_long_entry_grid_spacing_we_weight != (self.config.optimize.bounds.long_entry_grid_spacing_we_weight_0, self.config.optimize.bounds.long_entry_grid_spacing_we_weight_1):
                self.config.optimize.bounds.long_entry_grid_spacing_we_weight_0 = st.session_state.edit_opt_v7_long_entry_grid_spacing_we_weight[0]
                self.config.optimize.bounds.long_entry_grid_spacing_we_weight_1 = st.session_state.edit_opt_v7_long_entry_grid_spacing_we_weight[1]
        else:
            st.session_state.edit_opt_v7_long_entry_grid_spacing_we_weight = (self.config.optimize.bounds.long_entry_grid_spacing_we_weight_0, self.config.optimize.bounds.long_entry_grid_spacing_we_weight_1)
        st.slider(
            "long_entry_grid_spacing_we_weight",
            min_value=Bounds.ENTRY_GRID_SPACING_WE_WEIGHT_MIN,
            max_value=Bounds.ENTRY_GRID_SPACING_WE_WEIGHT_MAX,
            step=Bounds.ENTRY_GRID_SPACING_WE_WEIGHT_STEP,
            format=Bounds.ENTRY_GRID_SPACING_WE_WEIGHT_FORMAT,
            key="edit_opt_v7_long_entry_grid_spacing_we_weight",
            help=pbgui_help.entry_grid_spacing)
    
    # long_entry_initial_ema_dist
    @st.fragment
    def fragment_long_entry_initial_ema_dist(self):
        if "edit_opt_v7_long_entry_initial_ema_dist" in st.session_state:
            if st.session_state.edit_opt_v7_long_entry_initial_ema_dist != (self.config.optimize.bounds.long_entry_initial_ema_dist_0, self.config.optimize.bounds.long_entry_initial_ema_dist_1):
                self.config.optimize.bounds.long_entry_initial_ema_dist_0 = st.session_state.edit_opt_v7_long_entry_initial_ema_dist[0]
                self.config.optimize.bounds.long_entry_initial_ema_dist_1 = st.session_state.edit_opt_v7_long_entry_initial_ema_dist[1]
        else:
            st.session_state.edit_opt_v7_long_entry_initial_ema_dist = (self.config.optimize.bounds.long_entry_initial_ema_dist_0, self.config.optimize.bounds.long_entry_initial_ema_dist_1)
        st.slider(
            "long_entry_initial_ema_dist",
            min_value=Bounds.ENTRY_INITIAL_EMA_DIST_MIN,
            max_value=Bounds.ENTRY_INITIAL_EMA_DIST_MAX,
            step=Bounds.ENTRY_INITIAL_EMA_DIST_STEP,
            format=Bounds.ENTRY_INITIAL_EMA_DIST_FORMAT,
            key="edit_opt_v7_long_entry_initial_ema_dist",
            help=pbgui_help.entry_initial_ema_dist)
    
    # long_entry_initial_qty_pct
    @st.fragment
    def fragment_long_entry_initial_qty_pct(self):
        if "edit_opt_v7_long_entry_initial_qty_pct" in st.session_state:
            if st.session_state.edit_opt_v7_long_entry_initial_qty_pct != (self.config.optimize.bounds.long_entry_initial_qty_pct_0, self.config.optimize.bounds.long_entry_initial_qty_pct_1):
                self.config.optimize.bounds.long_entry_initial_qty_pct_0 = st.session_state.edit_opt_v7_long_entry_initial_qty_pct[0]
                self.config.optimize.bounds.long_entry_initial_qty_pct_1 = st.session_state.edit_opt_v7_long_entry_initial_qty_pct[1]
        else:
            st.session_state.edit_opt_v7_long_entry_initial_qty_pct = (self.config.optimize.bounds.long_entry_initial_qty_pct_0, self.config.optimize.bounds.long_entry_initial_qty_pct_1)
        st.slider(
            "long_entry_initial_qty_pct",
            min_value=Bounds.ENTRY_INITIAL_QTY_PCT_MIN,
            max_value=Bounds.ENTRY_INITIAL_QTY_PCT_MAX,
            step=Bounds.ENTRY_INITIAL_QTY_PCT_STEP,
            format=Bounds.ENTRY_INITIAL_QTY_PCT_FORMAT,
            key="edit_opt_v7_long_entry_initial_qty_pct",
            help=pbgui_help.entry_initial_qty_pct)
    
    # long_entry_trailing_double_down_factor
    @st.fragment
    def fragment_long_entry_trailing_double_down_factor(self):
        if "edit_opt_v7_long_entry_trailing_double_down_factor" in st.session_state:
            if st.session_state.edit_opt_v7_long_entry_trailing_double_down_factor != (self.config.optimize.bounds.long_entry_trailing_double_down_factor_0, self.config.optimize.bounds.long_entry_trailing_double_down_factor_1):
                self.config.optimize.bounds.long_entry_trailing_double_down_factor_0 = st.session_state.edit_opt_v7_long_entry_trailing_double_down_factor[0]
                self.config.optimize.bounds.long_entry_trailing_double_down_factor_1 = st.session_state.edit_opt_v7_long_entry_trailing_double_down_factor[1]
        else:
            st.session_state.edit_opt_v7_long_entry_trailing_double_down_factor = (self.config.optimize.bounds.long_entry_trailing_double_down_factor_0, self.config.optimize.bounds.long_entry_trailing_double_down_factor_1)
        st.slider(
            "long_entry_trailing_double_down_factor",
            min_value=Bounds.ENTRY_TRAILING_DOUBLE_DOWN_FACTOR_MIN,
            max_value=Bounds.ENTRY_TRAILING_DOUBLE_DOWN_FACTOR_MAX,
            step=Bounds.ENTRY_TRAILING_DOUBLE_DOWN_FACTOR_STEP,
            format=Bounds.ENTRY_TRAILING_DOUBLE_DOWN_FACTOR_FORMAT,
            key="edit_opt_v7_long_entry_trailing_double_down_factor",
            help=pbgui_help.trailing_parameters)
    
    # long_entry_trailing_grid_ratio
    @st.fragment
    def fragment_long_entry_trailing_grid_ratio(self):
        if "edit_opt_v7_long_entry_trailing_grid_ratio" in st.session_state:
            if st.session_state.edit_opt_v7_long_entry_trailing_grid_ratio != (self.config.optimize.bounds.long_entry_trailing_grid_ratio_0, self.config.optimize.bounds.long_entry_trailing_grid_ratio_1):
                self.config.optimize.bounds.long_entry_trailing_grid_ratio_0 = st.session_state.edit_opt_v7_long_entry_trailing_grid_ratio[0]
                self.config.optimize.bounds.long_entry_trailing_grid_ratio_1 = st.session_state.edit_opt_v7_long_entry_trailing_grid_ratio[1]
        else:
            st.session_state.edit_opt_v7_long_entry_trailing_grid_ratio = (self.config.optimize.bounds.long_entry_trailing_grid_ratio_0, self.config.optimize.bounds.long_entry_trailing_grid_ratio_1)
        st.slider(
            "long_entry_trailing_grid_ratio",
            min_value=Bounds.ENTRY_TRAILING_GRID_RATIO_MIN,
            max_value=Bounds.ENTRY_TRAILING_GRID_RATIO_MAX,
            step=Bounds.ENTRY_TRAILING_GRID_RATIO_STEP,
            format=Bounds.ENTRY_TRAILING_GRID_RATIO_FORMAT,
            key="edit_opt_v7_long_entry_trailing_grid_ratio",
            help=pbgui_help.trailing_parameters)
    
    # long_entry_trailing_retracement_pct
    @st.fragment
    def fragment_long_entry_trailing_retracement_pct(self):
        if "edit_opt_v7_long_entry_trailing_retracement_pct" in st.session_state:
            if st.session_state.edit_opt_v7_long_entry_trailing_retracement_pct != (self.config.optimize.bounds.long_entry_trailing_retracement_pct_0, self.config.optimize.bounds.long_entry_trailing_retracement_pct_1):
                self.config.optimize.bounds.long_entry_trailing_retracement_pct_0 = st.session_state.edit_opt_v7_long_entry_trailing_retracement_pct[0]
                self.config.optimize.bounds.long_entry_trailing_retracement_pct_1 = st.session_state.edit_opt_v7_long_entry_trailing_retracement_pct[1]
        else:
            st.session_state.edit_opt_v7_long_entry_trailing_retracement_pct = (self.config.optimize.bounds.long_entry_trailing_retracement_pct_0, self.config.optimize.bounds.long_entry_trailing_retracement_pct_1)
        st.slider(
            "long_entry_trailing_retracement_pct",
            min_value=Bounds.ENTRY_TRAILING_RETRACEMENT_PCT_MIN,
            max_value=Bounds.ENTRY_TRAILING_RETRACEMENT_PCT_MAX,
            step=Bounds.ENTRY_TRAILING_RETRACEMENT_PCT_STEP,
            format=Bounds.ENTRY_TRAILING_RETRACEMENT_PCT_FORMAT,
            key="edit_opt_v7_long_entry_trailing_retracement_pct",
            help=pbgui_help.trailing_parameters)
    
    # long_entry_trailing_threshold_pct
    @st.fragment
    def fragment_long_entry_trailing_threshold_pct(self):
        if "edit_opt_v7_long_entry_trailing_threshold_pct" in st.session_state:
            if st.session_state.edit_opt_v7_long_entry_trailing_threshold_pct != (self.config.optimize.bounds.long_entry_trailing_threshold_pct_0, self.config.optimize.bounds.long_entry_trailing_threshold_pct_1):
                self.config.optimize.bounds.long_entry_trailing_threshold_pct_0 = st.session_state.edit_opt_v7_long_entry_trailing_threshold_pct[0]
                self.config.optimize.bounds.long_entry_trailing_threshold_pct_1 = st.session_state.edit_opt_v7_long_entry_trailing_threshold_pct[1]
        else:
            st.session_state.edit_opt_v7_long_entry_trailing_threshold_pct = (self.config.optimize.bounds.long_entry_trailing_threshold_pct_0, self.config.optimize.bounds.long_entry_trailing_threshold_pct_1)
        st.slider(
            "long_entry_trailing_threshold_pct",
            min_value=Bounds.ENTRY_TRAILING_THRESHOLD_PCT_MIN,
            max_value=Bounds.ENTRY_TRAILING_THRESHOLD_PCT_MAX,
            step=Bounds.ENTRY_TRAILING_THRESHOLD_PCT_STEP,
            format=Bounds.ENTRY_TRAILING_THRESHOLD_PCT_FORMAT,
            key="edit_opt_v7_long_entry_trailing_threshold_pct",
            help=pbgui_help.trailing_parameters)
    
    # long_filter_log_range_ema_span
    @st.fragment
    def fragment_long_filter_log_range_ema_span(self):
        if "edit_opt_v7_long_filter_log_range_ema_span" in st.session_state:
            if st.session_state.edit_opt_v7_long_filter_log_range_ema_span != (self.config.optimize.bounds.long_filter_log_range_ema_span_0, self.config.optimize.bounds.long_filter_log_range_ema_span_1):
                self.config.optimize.bounds.long_filter_log_range_ema_span_0 = st.session_state.edit_opt_v7_long_filter_log_range_ema_span[0]
                self.config.optimize.bounds.long_filter_log_range_ema_span_1 = st.session_state.edit_opt_v7_long_filter_log_range_ema_span[1]
        else:
            st.session_state.edit_opt_v7_long_filter_log_range_ema_span = (self.config.optimize.bounds.long_filter_log_range_ema_span_0, self.config.optimize.bounds.long_filter_log_range_ema_span_1)
        st.slider(
            "long_filter_log_range_ema_span",
            min_value=Bounds.FILTER_LOG_RANGE_EMA_SPAN_MIN,
            max_value=Bounds.FILTER_LOG_RANGE_EMA_SPAN_MAX,
            step=Bounds.FILTER_LOG_RANGE_EMA_SPAN_STEP,
            format=Bounds.FILTER_LOG_RANGE_EMA_SPAN_FORMAT,
            key="edit_opt_v7_long_filter_log_range_ema_span",
            help=pbgui_help.filter_rolling_window)

    # long_filter_volume_drop_pct
    @st.fragment
    def fragment_long_filter_volume_drop_pct(self):
        if "edit_opt_v7_long_filter_volume_drop_pct" in st.session_state:
            if st.session_state.edit_opt_v7_long_filter_volume_drop_pct != (self.config.optimize.bounds.long_filter_volume_drop_pct_0, self.config.optimize.bounds.long_filter_volume_drop_pct_1):
                self.config.optimize.bounds.long_filter_volume_drop_pct_0 = st.session_state.edit_opt_v7_long_filter_volume_drop_pct[0]
                self.config.optimize.bounds.long_filter_volume_drop_pct_1 = st.session_state.edit_opt_v7_long_filter_volume_drop_pct[1]
        else:
            st.session_state.edit_opt_v7_long_filter_volume_drop_pct = (self.config.optimize.bounds.long_filter_volume_drop_pct_0, self.config.optimize.bounds.long_filter_volume_drop_pct_1)
        st.slider(
            "long_filter_volume_drop_pct",
            min_value=Bounds.FILTER_VOLUME_DROP_PCT_MIN,
            max_value=Bounds.FILTER_VOLUME_DROP_PCT_MAX,
            step=Bounds.FILTER_VOLUME_DROP_PCT_STEP,
            format=Bounds.FILTER_VOLUME_DROP_PCT_FORMAT,
            key="edit_opt_v7_long_filter_volume_drop_pct",
            help=pbgui_help.filter_volume_drop_pct)
    
    # long_filter_volume_ema_span
    @st.fragment
    def fragment_long_filter_volume_ema_span(self):
        if "edit_opt_v7_long_filter_volume_ema_span" in st.session_state:
            if st.session_state.edit_opt_v7_long_filter_volume_ema_span != (self.config.optimize.bounds.long_filter_volume_ema_span_0, self.config.optimize.bounds.long_filter_volume_ema_span_1):
                self.config.optimize.bounds.long_filter_volume_ema_span_0 = st.session_state.edit_opt_v7_long_filter_volume_ema_span[0]
                self.config.optimize.bounds.long_filter_volume_ema_span_1 = st.session_state.edit_opt_v7_long_filter_volume_ema_span[1]
        else:
            st.session_state.edit_opt_v7_long_filter_volume_ema_span = (self.config.optimize.bounds.long_filter_volume_ema_span_0, self.config.optimize.bounds.long_filter_volume_ema_span_1)
        st.slider(
            "long_filter_volume_ema_span",
            min_value=Bounds.FILTER_VOLUME_EMA_SPAN_MIN,
            max_value=Bounds.FILTER_VOLUME_EMA_SPAN_MAX,
            step=Bounds.FILTER_VOLUME_EMA_SPAN_STEP,
            format=Bounds.FILTER_VOLUME_EMA_SPAN_FORMAT,
            key="edit_opt_v7_long_filter_volume_ema_span",
            help=pbgui_help.filter_rolling_window)

    # long_n_positions
    @st.fragment
    def fragment_long_n_positions(self):
        if "edit_opt_v7_long_n_positions" in st.session_state:
            if st.session_state.edit_opt_v7_long_n_positions != (self.config.optimize.bounds.long_n_positions_0, self.config.optimize.bounds.long_n_positions_1):
                self.config.optimize.bounds.long_n_positions_0 = st.session_state.edit_opt_v7_long_n_positions[0]
                self.config.optimize.bounds.long_n_positions_1 = st.session_state.edit_opt_v7_long_n_positions[1]
        else:
            st.session_state.edit_opt_v7_long_n_positions = (self.config.optimize.bounds.long_n_positions_0, self.config.optimize.bounds.long_n_positions_1)
        st.slider(
            "long_n_positions",
            min_value=Bounds.N_POSITIONS_MIN,
            max_value=Bounds.N_POSITIONS_MAX,
            step=Bounds.N_POSITIONS_STEP,
            format=Bounds.N_POSITIONS_FORMAT,
            key="edit_opt_v7_long_n_positions",
            help=pbgui_help.n_positions)
    
    # long_total_wallet_exposure_limit
    @st.fragment
    def fragment_long_total_wallet_exposure_limit(self):
        if "edit_opt_v7_long_total_wallet_exposure_limit" in st.session_state:
            if st.session_state.edit_opt_v7_long_total_wallet_exposure_limit != (self.config.optimize.bounds.long_total_wallet_exposure_limit_0, self.config.optimize.bounds.long_total_wallet_exposure_limit_1):
                self.config.optimize.bounds.long_total_wallet_exposure_limit_0 = st.session_state.edit_opt_v7_long_total_wallet_exposure_limit[0]
                self.config.optimize.bounds.long_total_wallet_exposure_limit_1 = st.session_state.edit_opt_v7_long_total_wallet_exposure_limit[1]
        else:
            st.session_state.edit_opt_v7_long_total_wallet_exposure_limit = (self.config.optimize.bounds.long_total_wallet_exposure_limit_0, self.config.optimize.bounds.long_total_wallet_exposure_limit_1)
        st.slider(
            "long_total_wallet_exposure_limit",
            min_value=Bounds.TOTAL_WALLET_EXPOSURE_LIMIT_MIN,
            max_value=Bounds.TOTAL_WALLET_EXPOSURE_LIMIT_MAX,
            step=Bounds.TOTAL_WALLET_EXPOSURE_LIMIT_STEP,
            format=Bounds.TOTAL_WALLET_EXPOSURE_LIMIT_FORMAT,
            key="edit_opt_v7_long_total_wallet_exposure_limit",
            help=pbgui_help.total_wallet_exposure_limit)

    # long_unstuck_close_pct
    @st.fragment
    def fragment_long_unstuck_close_pct(self):
        if "edit_opt_v7_long_unstuck_close_pct" in st.session_state:
            if st.session_state.edit_opt_v7_long_unstuck_close_pct != (self.config.optimize.bounds.long_unstuck_close_pct_0, self.config.optimize.bounds.long_unstuck_close_pct_1):
                self.config.optimize.bounds.long_unstuck_close_pct_0 = st.session_state.edit_opt_v7_long_unstuck_close_pct[0]
                self.config.optimize.bounds.long_unstuck_close_pct_1 = st.session_state.edit_opt_v7_long_unstuck_close_pct[1]
        else:
            st.session_state.edit_opt_v7_long_unstuck_close_pct = (self.config.optimize.bounds.long_unstuck_close_pct_0, self.config.optimize.bounds.long_unstuck_close_pct_1)
        st.slider(
            "long_unstuck_close_pct",
            min_value=Bounds.UNSTUCK_CLOSE_PCT_MIN,
            max_value=Bounds.UNSTUCK_CLOSE_PCT_MAX,
            step=Bounds.UNSTUCK_CLOSE_PCT_STEP,
            format=Bounds.UNSTUCK_CLOSE_PCT_FORMAT,
            key="edit_opt_v7_long_unstuck_close_pct",
            help=pbgui_help.unstuck_close_pct)

    # long_unstuck_ema_dist
    @st.fragment
    def fragment_long_unstuck_ema_dist(self):
        if "edit_opt_v7_long_unstuck_ema_dist" in st.session_state:
            if st.session_state.edit_opt_v7_long_unstuck_ema_dist != (self.config.optimize.bounds.long_unstuck_ema_dist_0, self.config.optimize.bounds.long_unstuck_ema_dist_1):
                self.config.optimize.bounds.long_unstuck_ema_dist_0 = st.session_state.edit_opt_v7_long_unstuck_ema_dist[0]
                self.config.optimize.bounds.long_unstuck_ema_dist_1 = st.session_state.edit_opt_v7_long_unstuck_ema_dist[1]
        else:
            st.session_state.edit_opt_v7_long_unstuck_ema_dist = (self.config.optimize.bounds.long_unstuck_ema_dist_0, self.config.optimize.bounds.long_unstuck_ema_dist_1)
        st.slider(
            "long_unstuck_ema_dist",
            min_value=Bounds.UNSTUCK_EMA_DIST_MIN,
            max_value=Bounds.UNSTUCK_EMA_DIST_MAX,
            step=Bounds.UNSTUCK_EMA_DIST_STEP,
            format=Bounds.UNSTUCK_EMA_DIST_FORMAT,
            key="edit_opt_v7_long_unstuck_ema_dist",
            help=pbgui_help.unstuck_ema_dist)

    # long_unstuck_loss_allowance_pct
    @st.fragment
    def fragment_long_unstuck_loss_allowance_pct(self):
        if "edit_opt_v7_long_unstuck_loss_allowance_pct" in st.session_state:
            if st.session_state.edit_opt_v7_long_unstuck_loss_allowance_pct != (self.config.optimize.bounds.long_unstuck_loss_allowance_pct_0, self.config.optimize.bounds.long_unstuck_loss_allowance_pct_1):
                self.config.optimize.bounds.long_unstuck_loss_allowance_pct_0 = st.session_state.edit_opt_v7_long_unstuck_loss_allowance_pct[0]
                self.config.optimize.bounds.long_unstuck_loss_allowance_pct_1 = st.session_state.edit_opt_v7_long_unstuck_loss_allowance_pct[1]
        else:
            st.session_state.edit_opt_v7_long_unstuck_loss_allowance_pct = (self.config.optimize.bounds.long_unstuck_loss_allowance_pct_0, self.config.optimize.bounds.long_unstuck_loss_allowance_pct_1)
        st.slider(
            "long_unstuck_loss_allowance_pct",
            min_value=Bounds.UNSTUCK_LOSS_ALLOWANCE_PCT_MIN,
            max_value=Bounds.UNSTUCK_LOSS_ALLOWANCE_PCT_MAX,
            step=Bounds.UNSTUCK_LOSS_ALLOWANCE_PCT_STEP,
            format=Bounds.UNSTUCK_LOSS_ALLOWANCE_PCT_FORMAT,
            key="edit_opt_v7_long_unstuck_loss_allowance_pct",
            help=pbgui_help.unstuck_loss_allowance_pct)

    # long_unstuck_threshold
    @st.fragment
    def fragment_long_unstuck_threshold(self):
        if "edit_opt_v7_long_unstuck_threshold" in st.session_state:
            if st.session_state.edit_opt_v7_long_unstuck_threshold != (self.config.optimize.bounds.long_unstuck_threshold_0, self.config.optimize.bounds.long_unstuck_threshold_1):
                self.config.optimize.bounds.long_unstuck_threshold_0 = st.session_state.edit_opt_v7_long_unstuck_threshold[0]
                self.config.optimize.bounds.long_unstuck_threshold_1 = st.session_state.edit_opt_v7_long_unstuck_threshold[1]
        else:
            st.session_state.edit_opt_v7_long_unstuck_threshold = (self.config.optimize.bounds.long_unstuck_threshold_0, self.config.optimize.bounds.long_unstuck_threshold_1)
        st.slider(
            "long_unstuck_threshold",
            min_value=Bounds.UNSTUCK_THRESHOLD_MIN,
            max_value=Bounds.UNSTUCK_THRESHOLD_MAX,
            step=Bounds.UNSTUCK_THRESHOLD_STEP,
            format=Bounds.UNSTUCK_THRESHOLD_FORMAT,
            key="edit_opt_v7_long_unstuck_threshold",
            help=pbgui_help.unstuck_threshold)

    # # short_close_grid_markup_range
    # @st.fragment
    # def fragment_short_close_grid_markup_range(self):
    #     if "edit_opt_v7_short_close_grid_markup_range" in st.session_state:
    #         if st.session_state.edit_opt_v7_short_close_grid_markup_range != (self.config.optimize.bounds.short_close_grid_markup_range_0, self.config.optimize.bounds.short_close_grid_markup_range_1):
    #             self.config.optimize.bounds.short_close_grid_markup_range_0 = st.session_state.edit_opt_v7_short_close_grid_markup_range[0]
    #             self.config.optimize.bounds.short_close_grid_markup_range_1 = st.session_state.edit_opt_v7_short_close_grid_markup_range[1]
    #     else:
    #         st.session_state.edit_opt_v7_short_close_grid_markup_range = (self.config.optimize.bounds.short_close_grid_markup_range_0, self.config.optimize.bounds.short_close_grid_markup_range_1)
    #     st.slider(
    #         "short_close_grid_markup_range",
    #         min_value=Bounds.CLOSE_GRID_MARKUP_RANGE_MIN,
    #         max_value=Bounds.CLOSE_GRID_MARKUP_RANGE_MAX,
    #         step=Bounds.CLOSE_GRID_MARKUP_RANGE_STEP,
    #         format=Bounds.CLOSE_GRID_MARKUP_RANGE_FORMAT,
    #         key="edit_opt_v7_short_close_grid_markup_range",
    #         help=pbgui_help.close_grid_parameters)
    
    # # short_close_grid_min_markup
    # @st.fragment
    # def fragment_short_close_grid_min_markup(self):
    #     if "edit_opt_v7_short_close_grid_min_markup" in st.session_state:
    #         if st.session_state.edit_opt_v7_short_close_grid_min_markup != (self.config.optimize.bounds.short_close_grid_min_markup_0, self.config.optimize.bounds.short_close_grid_min_markup_1):
    #             self.config.optimize.bounds.short_close_grid_min_markup_0 = st.session_state.edit_opt_v7_short_close_grid_min_markup[0]
    #             self.config.optimize.bounds.short_close_grid_min_markup_1 = st.session_state.edit_opt_v7_short_close_grid_min_markup[1]
    #     else:
    #         st.session_state.edit_opt_v7_short_close_grid_min_markup = (self.config.optimize.bounds.short_close_grid_min_markup_0, self.config.optimize.bounds.short_close_grid_min_markup_1)
    #     st.slider(
    #         "short_close_grid_min_markup",
    #         min_value=Bounds.CLOSE_GRID_MIN_MARKUP_MIN,
    #         max_value=Bounds.CLOSE_GRID_MIN_MARKUP_MAX,
    #         step=Bounds.CLOSE_GRID_MIN_MARKUP_STEP,
    #         format=Bounds.CLOSE_GRID_MIN_MARKUP_FORMAT,
    #         key="edit_opt_v7_short_close_grid_min_markup",
    #         help=pbgui_help.close_grid_parameters)
    
    # short_close_grid_markup_end
    @st.fragment
    def fragment_short_close_grid_markup_end(self):
        if "edit_opt_v7_short_close_grid_markup_end" in st.session_state:
            if st.session_state.edit_opt_v7_short_close_grid_markup_end != (self.config.optimize.bounds.short_close_grid_markup_end_0, self.config.optimize.bounds.short_close_grid_markup_end_1):
                self.config.optimize.bounds.short_close_grid_markup_end_0 = st.session_state.edit_opt_v7_short_close_grid_markup_end[0]
                self.config.optimize.bounds.short_close_grid_markup_end_1 = st.session_state.edit_opt_v7_short_close_grid_markup_end[1]
        else:
            st.session_state.edit_opt_v7_short_close_grid_markup_end = (self.config.optimize.bounds.short_close_grid_markup_end_0, self.config.optimize.bounds.short_close_grid_markup_end_1)
        st.slider(
            "short_close_grid_markup_end",
            min_value=Bounds.CLOSE_GRID_MARKUP_END_MIN,
            max_value=Bounds.CLOSE_GRID_MARKUP_END_MAX,
            step=Bounds.CLOSE_GRID_MARKUP_END_STEP,
            format=Bounds.CLOSE_GRID_MARKUP_END_FORMAT,
            key="edit_opt_v7_short_close_grid_markup_end",
            help=pbgui_help.close_grid_parameters)
    
    # short_close_grid_markup_start
    @st.fragment
    def fragment_short_close_grid_markup_start(self):
        if "edit_opt_v7_short_close_grid_markup_start" in st.session_state:
            if st.session_state.edit_opt_v7_short_close_grid_markup_start != (self.config.optimize.bounds.short_close_grid_markup_start_0, self.config.optimize.bounds.short_close_grid_markup_start_1):
                self.config.optimize.bounds.short_close_grid_markup_start_0 = st.session_state.edit_opt_v7_short_close_grid_markup_start[0]
                self.config.optimize.bounds.short_close_grid_markup_start_1 = st.session_state.edit_opt_v7_short_close_grid_markup_start[1]
        else:
            st.session_state.edit_opt_v7_short_close_grid_markup_start = (self.config.optimize.bounds.short_close_grid_markup_start_0, self.config.optimize.bounds.short_close_grid_markup_start_1)
        st.slider(
            "short_close_grid_markup_start",
            min_value=Bounds.CLOSE_GRID_MARKUP_START_MIN,
            max_value=Bounds.CLOSE_GRID_MARKUP_START_MAX,
            step=Bounds.CLOSE_GRID_MARKUP_START_STEP,
            format=Bounds.CLOSE_GRID_MARKUP_START_FORMAT,
            key="edit_opt_v7_short_close_grid_markup_start",
            help=pbgui_help.close_grid_parameters)

    # short_close_grid_qty_pct
    @st.fragment
    def fragment_short_close_grid_qty_pct(self):
        if "edit_opt_v7_short_close_grid_qty_pct" in st.session_state:
            if st.session_state.edit_opt_v7_short_close_grid_qty_pct != (self.config.optimize.bounds.short_close_grid_qty_pct_0, self.config.optimize.bounds.short_close_grid_qty_pct_1):
                self.config.optimize.bounds.short_close_grid_qty_pct_0 = st.session_state.edit_opt_v7_short_close_grid_qty_pct[0]
                self.config.optimize.bounds.short_close_grid_qty_pct_1 = st.session_state.edit_opt_v7_short_close_grid_qty_pct[1]
        else:
            st.session_state.edit_opt_v7_short_close_grid_qty_pct = (self.config.optimize.bounds.short_close_grid_qty_pct_0, self.config.optimize.bounds.short_close_grid_qty_pct_1)
        st.slider(
            "short_close_grid_qty_pct",
            min_value=Bounds.CLOSE_GRID_QTY_PCT_MIN,
            max_value=Bounds.CLOSE_GRID_QTY_PCT_MAX,
            step=Bounds.CLOSE_GRID_QTY_PCT_STEP,
            format=Bounds.CLOSE_GRID_QTY_PCT_FORMAT,
            key="edit_opt_v7_short_close_grid_qty_pct",
            help=pbgui_help.close_grid_parameters)

    # short_close_trailing_grid_ratio
    @st.fragment
    def fragment_short_close_trailing_grid_ratio(self):
        if "edit_opt_v7_short_close_trailing_grid_ratio" in st.session_state:
            if st.session_state.edit_opt_v7_short_close_trailing_grid_ratio != (self.config.optimize.bounds.short_close_trailing_grid_ratio_0, self.config.optimize.bounds.short_close_trailing_grid_ratio_1):
                self.config.optimize.bounds.short_close_trailing_grid_ratio_0 = st.session_state.edit_opt_v7_short_close_trailing_grid_ratio[0]
                self.config.optimize.bounds.short_close_trailing_grid_ratio_1 = st.session_state.edit_opt_v7_short_close_trailing_grid_ratio[1]
        else:
            st.session_state.edit_opt_v7_short_close_trailing_grid_ratio = (self.config.optimize.bounds.short_close_trailing_grid_ratio_0, self.config.optimize.bounds.short_close_trailing_grid_ratio_1)
        st.slider(
            "short_close_trailing_grid_ratio",
            min_value=Bounds.CLOSE_TRAILING_GRID_RATIO_MIN,
            max_value=Bounds.CLOSE_TRAILING_GRID_RATIO_MAX,
            step=Bounds.CLOSE_TRAILING_GRID_RATIO_STEP,
            format=Bounds.CLOSE_TRAILING_GRID_RATIO_FORMAT,
            key="edit_opt_v7_short_close_trailing_grid_ratio",
            help=pbgui_help.close_grid_parameters)
    
    # short_close_trailing_qty_pct
    @st.fragment
    def fragment_short_close_trailing_qty_pct(self):
        if "edit_opt_v7_short_close_trailing_qty_pct" in st.session_state:
            if st.session_state.edit_opt_v7_short_close_trailing_qty_pct != (self.config.optimize.bounds.short_close_trailing_qty_pct_0, self.config.optimize.bounds.short_close_trailing_qty_pct_1):
                self.config.optimize.bounds.short_close_trailing_qty_pct_0 = st.session_state.edit_opt_v7_short_close_trailing_qty_pct[0]
                self.config.optimize.bounds.short_close_trailing_qty_pct_1 = st.session_state.edit_opt_v7_short_close_trailing_qty_pct[1]
        else:
            st.session_state.edit_opt_v7_short_close_trailing_qty_pct = (self.config.optimize.bounds.short_close_trailing_qty_pct_0, self.config.optimize.bounds.short_close_trailing_qty_pct_1)
        st.slider(
            "short_close_trailing_qty_pct",
            min_value=Bounds.CLOSE_TRAILING_QTY_PCT_MIN,
            max_value=Bounds.CLOSE_TRAILING_QTY_PCT_MAX,
            step=Bounds.CLOSE_TRAILING_QTY_PCT_STEP,
            format=Bounds.CLOSE_TRAILING_QTY_PCT_FORMAT,
            key="edit_opt_v7_short_close_trailing_qty_pct",
            help=pbgui_help.trailing_parameters)
    
    # short_close_trailing_retracement_pct
    @st.fragment
    def fragment_short_close_trailing_retracement_pct(self):
        if "edit_opt_v7_short_close_trailing_retracement_pct" in st.session_state:
            if st.session_state.edit_opt_v7_short_close_trailing_retracement_pct != (self.config.optimize.bounds.short_close_trailing_retracement_pct_0, self.config.optimize.bounds.short_close_trailing_retracement_pct_1):
                self.config.optimize.bounds.short_close_trailing_retracement_pct_0 = st.session_state.edit_opt_v7_short_close_trailing_retracement_pct[0]
                self.config.optimize.bounds.short_close_trailing_retracement_pct_1 = st.session_state.edit_opt_v7_short_close_trailing_retracement_pct[1]
        else:
            st.session_state.edit_opt_v7_short_close_trailing_retracement_pct = (self.config.optimize.bounds.short_close_trailing_retracement_pct_0, self.config.optimize.bounds.short_close_trailing_retracement_pct_1)
        st.slider(
            "short_close_trailing_retracement_pct",
            min_value=Bounds.CLOSE_TRAILING_RETRACEMENT_PCT_MIN,
            max_value=Bounds.CLOSE_TRAILING_RETRACEMENT_PCT_MAX,
            step=Bounds.CLOSE_TRAILING_RETRACEMENT_PCT_STEP,
            format=Bounds.CLOSE_TRAILING_RETRACEMENT_PCT_FORMAT,
            key="edit_opt_v7_short_close_trailing_retracement_pct",
            help=pbgui_help.trailing_parameters)
    
    # short_close_trailing_threshold_pct
    @st.fragment
    def fragment_short_close_trailing_threshold_pct(self):
        if "edit_opt_v7_short_close_trailing_threshold_pct" in st.session_state:
            if st.session_state.edit_opt_v7_short_close_trailing_threshold_pct != (self.config.optimize.bounds.short_close_trailing_threshold_pct_0, self.config.optimize.bounds.short_close_trailing_threshold_pct_1):
                self.config.optimize.bounds.short_close_trailing_threshold_pct_0 = st.session_state.edit_opt_v7_short_close_trailing_threshold_pct[0]
                self.config.optimize.bounds.short_close_trailing_threshold_pct_1 = st.session_state.edit_opt_v7_short_close_trailing_threshold_pct[1]
        else:
            st.session_state.edit_opt_v7_short_close_trailing_threshold_pct = (self.config.optimize.bounds.short_close_trailing_threshold_pct_0, self.config.optimize.bounds.short_close_trailing_threshold_pct_1)
        st.slider(
            "short_close_trailing_threshold_pct",
            min_value=Bounds.CLOSE_TRAILING_THRESHOLD_PCT_MIN,
            max_value=Bounds.CLOSE_TRAILING_THRESHOLD_PCT_MAX,
            step=Bounds.CLOSE_TRAILING_THRESHOLD_PCT_STEP,
            format=Bounds.CLOSE_TRAILING_THRESHOLD_PCT_FORMAT,
            key="edit_opt_v7_short_close_trailing_threshold_pct",
            help=pbgui_help.trailing_parameters)
    
    # short_ema_span_0
    @st.fragment
    def fragment_short_ema_span_0(self):
        if "edit_opt_v7_short_ema_span_0" in st.session_state:
            if st.session_state.edit_opt_v7_short_ema_span_0 != (self.config.optimize.bounds.short_ema_span_0_0, self.config.optimize.bounds.short_ema_span_0_1):
                self.config.optimize.bounds.short_ema_span_0_0 = st.session_state.edit_opt_v7_short_ema_span_0[0]
                self.config.optimize.bounds.short_ema_span_0_1 = st.session_state.edit_opt_v7_short_ema_span_0[1]
        else:
            st.session_state.edit_opt_v7_short_ema_span_0 = (self.config.optimize.bounds.short_ema_span_0_0, self.config.optimize.bounds.short_ema_span_0_1)
        st.slider(
            "short_ema_span_0",
            min_value=Bounds.EMA_SPAN_0_MIN,
            max_value=Bounds.EMA_SPAN_0_MAX,
            step=Bounds.EMA_SPAN_0_STEP,
            format=Bounds.EMA_SPAN_0_FORMAT,
            key="edit_opt_v7_short_ema_span_0",
            help=pbgui_help.ema_span)
    
    # short_ema_span_1
    @st.fragment
    def fragment_short_ema_span_1(self):
        if "edit_opt_v7_short_ema_span_1" in st.session_state:
            if st.session_state.edit_opt_v7_short_ema_span_1 != (self.config.optimize.bounds.short_ema_span_1_0, self.config.optimize.bounds.short_ema_span_1_1):
                self.config.optimize.bounds.short_ema_span_1_0 = st.session_state.edit_opt_v7_short_ema_span_1[0]
                self.config.optimize.bounds.short_ema_span_1_1 = st.session_state.edit_opt_v7_short_ema_span_1[1]
        else:
            st.session_state.edit_opt_v7_short_ema_span_1 = (self.config.optimize.bounds.short_ema_span_1_0, self.config.optimize.bounds.short_ema_span_1_1)
        st.slider(
            "short_ema_span_1",
            min_value=Bounds.EMA_SPAN_1_MIN,
            max_value=Bounds.EMA_SPAN_1_MAX,
            step=Bounds.EMA_SPAN_1_STEP,
            format=Bounds.EMA_SPAN_1_FORMAT,
            key="edit_opt_v7_short_ema_span_1",
            help=pbgui_help.ema_span)
    
    # short_entry_grid_double_down_factor
    @st.fragment
    def fragment_short_entry_grid_double_down_factor(self):
        if "edit_opt_v7_short_entry_grid_double_down_factor" in st.session_state:
            if st.session_state.edit_opt_v7_short_entry_grid_double_down_factor != (self.config.optimize.bounds.short_entry_grid_double_down_factor_0, self.config.optimize.bounds.short_entry_grid_double_down_factor_1):
                self.config.optimize.bounds.short_entry_grid_double_down_factor_0 = st.session_state.edit_opt_v7_short_entry_grid_double_down_factor[0]
                self.config.optimize.bounds.short_entry_grid_double_down_factor_1 = st.session_state.edit_opt_v7_short_entry_grid_double_down_factor[1]
        else:
            st.session_state.edit_opt_v7_short_entry_grid_double_down_factor = (self.config.optimize.bounds.short_entry_grid_double_down_factor_0, self.config.optimize.bounds.short_entry_grid_double_down_factor_1)
        st.slider(
            "short_entry_grid_double_down_factor",
            min_value=Bounds.ENTRY_GRID_DOUBLE_DOWN_FACTOR_MIN,
            max_value=Bounds.ENTRY_GRID_DOUBLE_DOWN_FACTOR_MAX,
            step=Bounds.ENTRY_GRID_DOUBLE_DOWN_FACTOR_STEP,
            format=Bounds.ENTRY_GRID_DOUBLE_DOWN_FACTOR_FORMAT,
            key="edit_opt_v7_short_entry_grid_double_down_factor",
            help=pbgui_help.entry_grid_double_down_factor)

    # short_entry_grid_spacing_log_span_hours
    @st.fragment
    def fragment_short_entry_grid_spacing_log_span_hours(self):
        if "edit_opt_v7_short_entry_grid_spacing_log_span_hours" in st.session_state:
            if st.session_state.edit_opt_v7_short_entry_grid_spacing_log_span_hours != (self.config.optimize.bounds.short_entry_grid_spacing_log_span_hours_0, self.config.optimize.bounds.short_entry_grid_spacing_log_span_hours_1):
                self.config.optimize.bounds.short_entry_grid_spacing_log_span_hours_0 = st.session_state.edit_opt_v7_short_entry_grid_spacing_log_span_hours[0]
                self.config.optimize.bounds.short_entry_grid_spacing_log_span_hours_1 = st.session_state.edit_opt_v7_short_entry_grid_spacing_log_span_hours[1]
        else:
            st.session_state.edit_opt_v7_short_entry_grid_spacing_log_span_hours = (self.config.optimize.bounds.short_entry_grid_spacing_log_span_hours_0, self.config.optimize.bounds.short_entry_grid_spacing_log_span_hours_1)
        st.slider(
            "short_entry_grid_spacing_log_span_hours",
            min_value=Bounds.ENTRY_GRID_SPACING_LOG_SPAN_HOURS_MIN,
            max_value=Bounds.ENTRY_GRID_SPACING_LOG_SPAN_HOURS_MAX,
            step=Bounds.ENTRY_GRID_SPACING_LOG_SPAN_HOURS_STEP,
            format=Bounds.ENTRY_GRID_SPACING_LOG_SPAN_HOURS_FORMAT,
            key="edit_opt_v7_short_entry_grid_spacing_log_span_hours",
            help=pbgui_help.entry_grid_spacing_log)

    # short_entry_grid_spacing_log_weight
    @st.fragment
    def fragment_short_entry_grid_spacing_log_weight(self):
        if "edit_opt_v7_short_entry_grid_spacing_log_weight" in st.session_state:
            if st.session_state.edit_opt_v7_short_entry_grid_spacing_log_weight != (self.config.optimize.bounds.short_entry_grid_spacing_log_weight_0, self.config.optimize.bounds.short_entry_grid_spacing_log_weight_1):
                self.config.optimize.bounds.short_entry_grid_spacing_log_weight_0 = st.session_state.edit_opt_v7_short_entry_grid_spacing_log_weight[0]
                self.config.optimize.bounds.short_entry_grid_spacing_log_weight_1 = st.session_state.edit_opt_v7_short_entry_grid_spacing_log_weight[1]
        else:
            st.session_state.edit_opt_v7_short_entry_grid_spacing_log_weight = (self.config.optimize.bounds.short_entry_grid_spacing_log_weight_0, self.config.optimize.bounds.short_entry_grid_spacing_log_weight_1)
        st.slider(
            "short_entry_grid_spacing_log_weight",
            min_value=Bounds.ENTRY_GRID_SPACING_LOG_WEIGHT_MIN,
            max_value=Bounds.ENTRY_GRID_SPACING_LOG_WEIGHT_MAX,
            step=Bounds.ENTRY_GRID_SPACING_LOG_WEIGHT_STEP,
            format=Bounds.ENTRY_GRID_SPACING_LOG_WEIGHT_FORMAT,
            key="edit_opt_v7_short_entry_grid_spacing_log_weight",
            help=pbgui_help.entry_grid_spacing_log)

    # short_entry_grid_spacing_pct
    @st.fragment
    def fragment_short_entry_grid_spacing_pct(self):
        if "edit_opt_v7_short_entry_grid_spacing_pct" in st.session_state:
            if st.session_state.edit_opt_v7_short_entry_grid_spacing_pct != (self.config.optimize.bounds.short_entry_grid_spacing_pct_0, self.config.optimize.bounds.short_entry_grid_spacing_pct_1):
                self.config.optimize.bounds.short_entry_grid_spacing_pct_0 = st.session_state.edit_opt_v7_short_entry_grid_spacing_pct[0]
                self.config.optimize.bounds.short_entry_grid_spacing_pct_1 = st.session_state.edit_opt_v7_short_entry_grid_spacing_pct[1]
        else:
            st.session_state.edit_opt_v7_short_entry_grid_spacing_pct = (self.config.optimize.bounds.short_entry_grid_spacing_pct_0, self.config.optimize.bounds.short_entry_grid_spacing_pct_1)
        st.slider(
            "short_entry_grid_spacing_pct",
            min_value=Bounds.ENTRY_GRID_SPACING_PCT_MIN,
            max_value=Bounds.ENTRY_GRID_SPACING_PCT_MAX,
            step=Bounds.ENTRY_GRID_SPACING_PCT_STEP,
            format=Bounds.ENTRY_GRID_SPACING_PCT_FORMAT,
            key="edit_opt_v7_short_entry_grid_spacing_pct",
            help=pbgui_help.entry_grid_spacing)
    
    # short_entry_grid_spacing_we_weight
    @st.fragment
    def fragment_short_entry_grid_spacing_we_weight(self):
        if "edit_opt_v7_short_entry_grid_spacing_we_weight" in st.session_state:
            if st.session_state.edit_opt_v7_short_entry_grid_spacing_we_weight != (self.config.optimize.bounds.short_entry_grid_spacing_we_weight_0, self.config.optimize.bounds.short_entry_grid_spacing_we_weight_1):
                self.config.optimize.bounds.short_entry_grid_spacing_we_weight_0 = st.session_state.edit_opt_v7_short_entry_grid_spacing_we_weight[0]
                self.config.optimize.bounds.short_entry_grid_spacing_we_weight_1 = st.session_state.edit_opt_v7_short_entry_grid_spacing_we_weight[1]
        else:
            st.session_state.edit_opt_v7_short_entry_grid_spacing_we_weight = (self.config.optimize.bounds.short_entry_grid_spacing_we_weight_0, self.config.optimize.bounds.short_entry_grid_spacing_we_weight_1)
        st.slider(
            "short_entry_grid_spacing_we_weight",
            min_value=Bounds.ENTRY_GRID_SPACING_WE_WEIGHT_MIN,
            max_value=Bounds.ENTRY_GRID_SPACING_WE_WEIGHT_MAX,
            step=Bounds.ENTRY_GRID_SPACING_WE_WEIGHT_STEP,
            format=Bounds.ENTRY_GRID_SPACING_WE_WEIGHT_FORMAT,
            key="edit_opt_v7_short_entry_grid_spacing_we_weight",
            help=pbgui_help.entry_grid_spacing)
    
    # short_entry_initial_ema_dist
    @st.fragment
    def fragment_short_entry_initial_ema_dist(self):
        if "edit_opt_v7_short_entry_initial_ema_dist" in st.session_state:
            if st.session_state.edit_opt_v7_short_entry_initial_ema_dist != (self.config.optimize.bounds.short_entry_initial_ema_dist_0, self.config.optimize.bounds.short_entry_initial_ema_dist_1):
                self.config.optimize.bounds.short_entry_initial_ema_dist_0 = st.session_state.edit_opt_v7_short_entry_initial_ema_dist[0]
                self.config.optimize.bounds.short_entry_initial_ema_dist_1 = st.session_state.edit_opt_v7_short_entry_initial_ema_dist[1]
        else:
            st.session_state.edit_opt_v7_short_entry_initial_ema_dist = (self.config.optimize.bounds.short_entry_initial_ema_dist_0, self.config.optimize.bounds.short_entry_initial_ema_dist_1)
        st.slider(
            "short_entry_initial_ema_dist",
            min_value=Bounds.ENTRY_INITIAL_EMA_DIST_MIN,
            max_value=Bounds.ENTRY_INITIAL_EMA_DIST_MAX,
            step=Bounds.ENTRY_INITIAL_EMA_DIST_STEP,
            format=Bounds.ENTRY_INITIAL_EMA_DIST_FORMAT,
            key="edit_opt_v7_short_entry_initial_ema_dist",
            help=pbgui_help.entry_initial_ema_dist)

    # short_entry_initial_qty_pct
    @st.fragment
    def fragment_short_entry_initial_qty_pct(self):
        if "edit_opt_v7_short_entry_initial_qty_pct" in st.session_state:
            if st.session_state.edit_opt_v7_short_entry_initial_qty_pct != (self.config.optimize.bounds.short_entry_initial_qty_pct_0, self.config.optimize.bounds.short_entry_initial_qty_pct_1):
                self.config.optimize.bounds.short_entry_initial_qty_pct_0 = st.session_state.edit_opt_v7_short_entry_initial_qty_pct[0]
                self.config.optimize.bounds.short_entry_initial_qty_pct_1 = st.session_state.edit_opt_v7_short_entry_initial_qty_pct[1]
        else:
            st.session_state.edit_opt_v7_short_entry_initial_qty_pct = (self.config.optimize.bounds.short_entry_initial_qty_pct_0, self.config.optimize.bounds.short_entry_initial_qty_pct_1)
        st.slider(
            "short_entry_initial_qty_pct",
            min_value=Bounds.ENTRY_INITIAL_QTY_PCT_MIN,
            max_value=Bounds.ENTRY_INITIAL_QTY_PCT_MAX,
            step=Bounds.ENTRY_INITIAL_QTY_PCT_STEP,
            format=Bounds.ENTRY_INITIAL_QTY_PCT_FORMAT,
            key="edit_opt_v7_short_entry_initial_qty_pct",
            help=pbgui_help.entry_initial_qty_pct)
    
    # short_entry_trailing_double_down_factor
    @st.fragment
    def fragment_short_entry_trailing_double_down_factor(self):
        if "edit_opt_v7_short_entry_trailing_double_down_factor" in st.session_state:
            if st.session_state.edit_opt_v7_short_entry_trailing_double_down_factor != (self.config.optimize.bounds.short_entry_trailing_double_down_factor_0, self.config.optimize.bounds.short_entry_trailing_double_down_factor_1):
                self.config.optimize.bounds.short_entry_trailing_double_down_factor_0 = st.session_state.edit_opt_v7_short_entry_trailing_double_down_factor[0]
                self.config.optimize.bounds.short_entry_trailing_double_down_factor_1 = st.session_state.edit_opt_v7_short_entry_trailing_double_down_factor[1]
        else:
            st.session_state.edit_opt_v7_short_entry_trailing_double_down_factor = (self.config.optimize.bounds.short_entry_trailing_double_down_factor_0, self.config.optimize.bounds.short_entry_trailing_double_down_factor_1)
        st.slider(
            "short_entry_trailing_double_down_factor",
            min_value=Bounds.ENTRY_TRAILING_DOUBLE_DOWN_FACTOR_MIN,
            max_value=Bounds.ENTRY_TRAILING_DOUBLE_DOWN_FACTOR_MAX,
            step=Bounds.ENTRY_TRAILING_DOUBLE_DOWN_FACTOR_STEP,
            format=Bounds.ENTRY_TRAILING_DOUBLE_DOWN_FACTOR_FORMAT,
            key="edit_opt_v7_short_entry_trailing_double_down_factor",
            help=pbgui_help.trailing_parameters)
    
    # short_entry_trailing_grid_ratio
    @st.fragment
    def fragment_short_entry_trailing_grid_ratio(self):
        if "edit_opt_v7_short_entry_trailing_grid_ratio" in st.session_state:
            if st.session_state.edit_opt_v7_short_entry_trailing_grid_ratio != (self.config.optimize.bounds.short_entry_trailing_grid_ratio_0, self.config.optimize.bounds.short_entry_trailing_grid_ratio_1):
                self.config.optimize.bounds.short_entry_trailing_grid_ratio_0 = st.session_state.edit_opt_v7_short_entry_trailing_grid_ratio[0]
                self.config.optimize.bounds.short_entry_trailing_grid_ratio_1 = st.session_state.edit_opt_v7_short_entry_trailing_grid_ratio[1]
        else:
            st.session_state.edit_opt_v7_short_entry_trailing_grid_ratio = (self.config.optimize.bounds.short_entry_trailing_grid_ratio_0, self.config.optimize.bounds.short_entry_trailing_grid_ratio_1)
        st.slider(
            "short_entry_trailing_grid_ratio",
            min_value=Bounds.ENTRY_TRAILING_GRID_RATIO_MIN,
            max_value=Bounds.ENTRY_TRAILING_GRID_RATIO_MAX,
            step=Bounds.ENTRY_TRAILING_GRID_RATIO_STEP,
            format=Bounds.ENTRY_TRAILING_GRID_RATIO_FORMAT,
            key="edit_opt_v7_short_entry_trailing_grid_ratio",
            help=pbgui_help.trailing_parameters)
    
    # short_entry_trailing_retracement_pct
    @st.fragment
    def fragment_short_entry_trailing_retracement_pct(self):
        if "edit_opt_v7_short_entry_trailing_retracement_pct" in st.session_state:
            if st.session_state.edit_opt_v7_short_entry_trailing_retracement_pct != (self.config.optimize.bounds.short_entry_trailing_retracement_pct_0, self.config.optimize.bounds.short_entry_trailing_retracement_pct_1):
                self.config.optimize.bounds.short_entry_trailing_retracement_pct_0 = st.session_state.edit_opt_v7_short_entry_trailing_retracement_pct[0]
                self.config.optimize.bounds.short_entry_trailing_retracement_pct_1 = st.session_state.edit_opt_v7_short_entry_trailing_retracement_pct[1]
        else:
            st.session_state.edit_opt_v7_short_entry_trailing_retracement_pct = (self.config.optimize.bounds.short_entry_trailing_retracement_pct_0, self.config.optimize.bounds.short_entry_trailing_retracement_pct_1)
        st.slider(
            "short_entry_trailing_retracement_pct",
            min_value=Bounds.ENTRY_TRAILING_RETRACEMENT_PCT_MIN,
            max_value=Bounds.ENTRY_TRAILING_RETRACEMENT_PCT_MAX,
            step=Bounds.ENTRY_TRAILING_RETRACEMENT_PCT_STEP,
            format=Bounds.ENTRY_TRAILING_RETRACEMENT_PCT_FORMAT,
            key="edit_opt_v7_short_entry_trailing_retracement_pct",
            help=pbgui_help.trailing_parameters)
    
    # short_entry_trailing_threshold_pct
    @st.fragment
    def fragment_short_entry_trailing_threshold_pct(self):
        if "edit_opt_v7_short_entry_trailing_threshold_pct" in st.session_state:
            if st.session_state.edit_opt_v7_short_entry_trailing_threshold_pct != (self.config.optimize.bounds.short_entry_trailing_threshold_pct_0, self.config.optimize.bounds.short_entry_trailing_threshold_pct_1):
                self.config.optimize.bounds.short_entry_trailing_threshold_pct_0 = st.session_state.edit_opt_v7_short_entry_trailing_threshold_pct[0]
                self.config.optimize.bounds.short_entry_trailing_threshold_pct_1 = st.session_state.edit_opt_v7_short_entry_trailing_threshold_pct[1]
        else:
            st.session_state.edit_opt_v7_short_entry_trailing_threshold_pct = (self.config.optimize.bounds.short_entry_trailing_threshold_pct_0, self.config.optimize.bounds.short_entry_trailing_threshold_pct_1)
        st.slider(
            "short_entry_trailing_threshold_pct",
            min_value=Bounds.ENTRY_TRAILING_THRESHOLD_PCT_MIN,
            max_value=Bounds.ENTRY_TRAILING_THRESHOLD_PCT_MAX,
            step=Bounds.ENTRY_TRAILING_THRESHOLD_PCT_STEP,
            format=Bounds.ENTRY_TRAILING_THRESHOLD_PCT_FORMAT,
            key="edit_opt_v7_short_entry_trailing_threshold_pct",
            help=pbgui_help.trailing_parameters)
    
    # short_filter_log_range_ema_span
    @st.fragment
    def fragment_short_filter_log_range_ema_span(self):
        if "edit_opt_v7_short_filter_log_range_ema_span" in st.session_state:
            if st.session_state.edit_opt_v7_short_filter_log_range_ema_span != (self.config.optimize.bounds.short_filter_log_range_ema_span_0, self.config.optimize.bounds.short_filter_log_range_ema_span_1):
                self.config.optimize.bounds.short_filter_log_range_ema_span_0 = st.session_state.edit_opt_v7_short_filter_log_range_ema_span[0]
                self.config.optimize.bounds.short_filter_log_range_ema_span_1 = st.session_state.edit_opt_v7_short_filter_log_range_ema_span[1]
        else:
            st.session_state.edit_opt_v7_short_filter_log_range_ema_span = (self.config.optimize.bounds.short_filter_log_range_ema_span_0, self.config.optimize.bounds.short_filter_log_range_ema_span_1)
        st.slider(
            "short_filter_log_range_ema_span",
            min_value=Bounds.FILTER_LOG_RANGE_EMA_SPAN_MIN,
            max_value=Bounds.FILTER_LOG_RANGE_EMA_SPAN_MAX,
            step=Bounds.FILTER_LOG_RANGE_EMA_SPAN_STEP,
            format=Bounds.FILTER_LOG_RANGE_EMA_SPAN_FORMAT,
            key="edit_opt_v7_short_filter_log_range_ema_span",
            help=pbgui_help.filter_rolling_window)
    
    # short_filter_volume_drop_pct
    @st.fragment
    def fragment_short_filter_volume_drop_pct(self):
        if "edit_opt_v7_short_filter_volume_drop_pct" in st.session_state:
            if st.session_state.edit_opt_v7_short_filter_volume_drop_pct != (self.config.optimize.bounds.short_filter_volume_drop_pct_0, self.config.optimize.bounds.short_filter_volume_drop_pct_1):
                self.config.optimize.bounds.short_filter_volume_drop_pct_0 = st.session_state.edit_opt_v7_short_filter_volume_drop_pct[0]
                self.config.optimize.bounds.short_filter_volume_drop_pct_1 = st.session_state.edit_opt_v7_short_filter_volume_drop_pct[1]
        else:
            st.session_state.edit_opt_v7_short_filter_volume_drop_pct = (self.config.optimize.bounds.short_filter_volume_drop_pct_0, self.config.optimize.bounds.short_filter_volume_drop_pct_1)
        st.slider(
            "short_filter_volume_drop_pct",
            min_value=Bounds.FILTER_VOLUME_DROP_PCT_MIN,
            max_value=Bounds.FILTER_VOLUME_DROP_PCT_MAX,
            step=Bounds.FILTER_VOLUME_DROP_PCT_STEP,
            format=Bounds.FILTER_VOLUME_DROP_PCT_FORMAT,
            key="edit_opt_v7_short_filter_volume_drop_pct",
            help=pbgui_help.filter_volume_drop_pct)

    # short_filter_volume_ema_span
    @st.fragment
    def fragment_short_filter_volume_ema_span(self):
        if "edit_opt_v7_short_filter_volume_ema_span" in st.session_state:
            if st.session_state.edit_opt_v7_short_filter_volume_ema_span != (self.config.optimize.bounds.short_filter_volume_ema_span_0, self.config.optimize.bounds.short_filter_volume_ema_span_1):
                self.config.optimize.bounds.short_filter_volume_ema_span_0 = st.session_state.edit_opt_v7_short_filter_volume_ema_span[0]
                self.config.optimize.bounds.short_filter_volume_ema_span_1 = st.session_state.edit_opt_v7_short_filter_volume_ema_span[1]
        else:
            st.session_state.edit_opt_v7_short_filter_volume_ema_span = (self.config.optimize.bounds.short_filter_volume_ema_span_0, self.config.optimize.bounds.short_filter_volume_ema_span_1)
        st.slider(
            "short_filter_volume_ema_span",
            min_value=Bounds.FILTER_VOLUME_EMA_SPAN_MIN,
            max_value=Bounds.FILTER_VOLUME_EMA_SPAN_MAX,
            step=Bounds.FILTER_VOLUME_EMA_SPAN_STEP,
            format=Bounds.FILTER_VOLUME_EMA_SPAN_FORMAT,
            key="edit_opt_v7_short_filter_volume_ema_span",
            help=pbgui_help.filter_rolling_window)

    # short_n_positions
    @st.fragment
    def fragment_short_n_positions(self):
        if "edit_opt_v7_short_n_positions" in st.session_state:
            if st.session_state.edit_opt_v7_short_n_positions != (self.config.optimize.bounds.short_n_positions_0, self.config.optimize.bounds.short_n_positions_1):
                self.config.optimize.bounds.short_n_positions_0 = st.session_state.edit_opt_v7_short_n_positions[0]
                self.config.optimize.bounds.short_n_positions_1 = st.session_state.edit_opt_v7_short_n_positions[1]
        else:
            st.session_state.edit_opt_v7_short_n_positions = (self.config.optimize.bounds.short_n_positions_0, self.config.optimize.bounds.short_n_positions_1)
        st.slider(
            "short_n_positions",
            min_value=Bounds.N_POSITIONS_MIN,
            max_value=Bounds.N_POSITIONS_MAX,
            step=Bounds.N_POSITIONS_STEP,
            format=Bounds.N_POSITIONS_FORMAT,
            key="edit_opt_v7_short_n_positions",
            help=pbgui_help.n_positions)
    
    # short_total_wallet_exposure_limit
    @st.fragment
    def fragment_short_total_wallet_exposure_limit(self):
        if "edit_opt_v7_short_total_wallet_exposure_limit" in st.session_state:
            if st.session_state.edit_opt_v7_short_total_wallet_exposure_limit != (self.config.optimize.bounds.short_total_wallet_exposure_limit_0, self.config.optimize.bounds.short_total_wallet_exposure_limit_1):
                self.config.optimize.bounds.short_total_wallet_exposure_limit_0 = st.session_state.edit_opt_v7_short_total_wallet_exposure_limit[0]
                self.config.optimize.bounds.short_total_wallet_exposure_limit_1 = st.session_state.edit_opt_v7_short_total_wallet_exposure_limit[1]
        else:
            st.session_state.edit_opt_v7_short_total_wallet_exposure_limit = (self.config.optimize.bounds.short_total_wallet_exposure_limit_0, self.config.optimize.bounds.short_total_wallet_exposure_limit_1)
        st.slider(
            "short_total_wallet_exposure_limit",
            min_value=Bounds.TOTAL_WALLET_EXPOSURE_LIMIT_MIN,
            max_value=Bounds.TOTAL_WALLET_EXPOSURE_LIMIT_MAX,
            step=Bounds.TOTAL_WALLET_EXPOSURE_LIMIT_STEP,
            format=Bounds.TOTAL_WALLET_EXPOSURE_LIMIT_FORMAT,
            key="edit_opt_v7_short_total_wallet_exposure_limit",
            help=pbgui_help.total_wallet_exposure_limit)
    
    # short_unstuck_close_pct
    @st.fragment
    def fragment_short_unstuck_close_pct(self):
        if "edit_opt_v7_short_unstuck_close_pct" in st.session_state:
            if st.session_state.edit_opt_v7_short_unstuck_close_pct != (self.config.optimize.bounds.short_unstuck_close_pct_0, self.config.optimize.bounds.short_unstuck_close_pct_1):
                self.config.optimize.bounds.short_unstuck_close_pct_0 = st.session_state.edit_opt_v7_short_unstuck_close_pct[0]
                self.config.optimize.bounds.short_unstuck_close_pct_1 = st.session_state.edit_opt_v7_short_unstuck_close_pct[1]
        else:
            st.session_state.edit_opt_v7_short_unstuck_close_pct = (self.config.optimize.bounds.short_unstuck_close_pct_0, self.config.optimize.bounds.short_unstuck_close_pct_1)
        st.slider(
            "short_unstuck_close_pct",
            min_value=Bounds.UNSTUCK_CLOSE_PCT_MIN,
            max_value=Bounds.UNSTUCK_CLOSE_PCT_MAX,
            step=Bounds.UNSTUCK_CLOSE_PCT_STEP,
            format=Bounds.UNSTUCK_CLOSE_PCT_FORMAT,
            key="edit_opt_v7_short_unstuck_close_pct",
            help=pbgui_help.unstuck_close_pct)

    # short_unstuck_ema_dist
    @st.fragment
    def fragment_short_unstuck_ema_dist(self):
        if "edit_opt_v7_short_unstuck_ema_dist" in st.session_state:
            if st.session_state.edit_opt_v7_short_unstuck_ema_dist != (self.config.optimize.bounds.short_unstuck_ema_dist_0, self.config.optimize.bounds.short_unstuck_ema_dist_1):
                self.config.optimize.bounds.short_unstuck_ema_dist_0 = st.session_state.edit_opt_v7_short_unstuck_ema_dist[0]
                self.config.optimize.bounds.short_unstuck_ema_dist_1 = st.session_state.edit_opt_v7_short_unstuck_ema_dist[1]
        else:
            st.session_state.edit_opt_v7_short_unstuck_ema_dist = (self.config.optimize.bounds.short_unstuck_ema_dist_0, self.config.optimize.bounds.short_unstuck_ema_dist_1)
        st.slider(
            "short_unstuck_ema_dist",
            min_value=Bounds.UNSTUCK_EMA_DIST_MIN,
            max_value=Bounds.UNSTUCK_EMA_DIST_MAX,
            step=Bounds.UNSTUCK_EMA_DIST_STEP,
            format=Bounds.UNSTUCK_EMA_DIST_FORMAT,
            key="edit_opt_v7_short_unstuck_ema_dist",
            help=pbgui_help.unstuck_ema_dist)
    
    # short_unstuck_loss_allowance_pct
    @st.fragment
    def fragment_short_unstuck_loss_allowance_pct(self):
        if "edit_opt_v7_short_unstuck_loss_allowance_pct" in st.session_state:
            if st.session_state.edit_opt_v7_short_unstuck_loss_allowance_pct != (self.config.optimize.bounds.short_unstuck_loss_allowance_pct_0, self.config.optimize.bounds.short_unstuck_loss_allowance_pct_1):
                self.config.optimize.bounds.short_unstuck_loss_allowance_pct_0 = st.session_state.edit_opt_v7_short_unstuck_loss_allowance_pct[0]
                self.config.optimize.bounds.short_unstuck_loss_allowance_pct_1 = st.session_state.edit_opt_v7_short_unstuck_loss_allowance_pct[1]
        else:
            st.session_state.edit_opt_v7_short_unstuck_loss_allowance_pct = (self.config.optimize.bounds.short_unstuck_loss_allowance_pct_0, self.config.optimize.bounds.short_unstuck_loss_allowance_pct_1)
        st.slider(
            "short_unstuck_loss_allowance_pct",
            min_value=Bounds.UNSTUCK_LOSS_ALLOWANCE_PCT_MIN,
            max_value=Bounds.UNSTUCK_LOSS_ALLOWANCE_PCT_MAX,
            step=Bounds.UNSTUCK_LOSS_ALLOWANCE_PCT_STEP,
            format=Bounds.UNSTUCK_LOSS_ALLOWANCE_PCT_FORMAT,
            key="edit_opt_v7_short_unstuck_loss_allowance_pct",
            help=pbgui_help.unstuck_loss_allowance_pct)
    
    # short_unstuck_threshold
    @st.fragment
    def fragment_short_unstuck_threshold(self):
        if "edit_opt_v7_short_unstuck_threshold" in st.session_state:
            if st.session_state.edit_opt_v7_short_unstuck_threshold != (self.config.optimize.bounds.short_unstuck_threshold_0, self.config.optimize.bounds.short_unstuck_threshold_1):
                self.config.optimize.bounds.short_unstuck_threshold_0 = st.session_state.edit_opt_v7_short_unstuck_threshold[0]
                self.config.optimize.bounds.short_unstuck_threshold_1 = st.session_state.edit_opt_v7_short_unstuck_threshold[1]
        else:
            st.session_state.edit_opt_v7_short_unstuck_threshold = (self.config.optimize.bounds.short_unstuck_threshold_0, self.config.optimize.bounds.short_unstuck_threshold_1)
        st.slider(
            "short_unstuck_threshold",
            min_value=Bounds.UNSTUCK_THRESHOLD_MIN,
            max_value=Bounds.UNSTUCK_THRESHOLD_MAX,
            step=Bounds.UNSTUCK_THRESHOLD_STEP,
            format=Bounds.UNSTUCK_THRESHOLD_FORMAT,
            key="edit_opt_v7_short_unstuck_threshold",
            help=pbgui_help.unstuck_threshold)

    @st.fragment
    def fragment_limits(self):
        LIMITS = [
            "adg",
            "adg_per_exposure_long",
            "adg_per_exposure_short",
            "adg_w",
            "adg_w_per_exposure_long",
            "adg_w_per_exposure_short",
            "calmar_ratio",
            "calmar_ratio_w",
            "drawdown_worst",
            "drawdown_worst_mean_1pct",
            "equity_balance_diff_neg_max",
            "equity_balance_diff_neg_mean",
            "equity_balance_diff_pos_max",
            "equity_balance_diff_pos_mean",
            "equity_choppiness",
            "equity_choppiness_w",
            "equity_jerkiness",
            "equity_jerkiness_w",
            "expected_shortfall_1pct",
            "exponential_fit_error",
            "exponential_fit_error_w",
            "gain",
            "gain_per_exposure_long",
            "gain_per_exposure_short",
            "loss_profit_ratio",
            "loss_profit_ratio_w",
            "mdg",
            "mdg_per_exposure_long",
            "mdg_per_exposure_short",
            "mdg_w",
            "mdg_w_per_exposure_long",
            "mdg_w_per_exposure_short",
            "omega_ratio",
            "omega_ratio_w",
            "position_held_hours_max",
            "position_held_hours_mean",
            "position_held_hours_median",
            "position_unchanged_hours_max",
            "positions_held_per_day",
            "sharpe_ratio",
            "sharpe_ratio_w",
            "sortino_ratio",
            "sortino_ratio_w",
            "sterling_ratio",
            "sterling_ratio_w",
            "volume_pct_per_day_avg",
            "volume_pct_per_day_avg_w",
        ]
        LIMITS_BTC = [
            "adg",
            "adg_per_exposure_long",
            "adg_per_exposure_short",
            "adg_w",
            "adg_w_per_exposure_long",
            "adg_w_per_exposure_short",
            "calmar_ratio",
            "calmar_ratio_w",
            "drawdown_worst",
            "drawdown_worst_mean_1pct",
            "equity_balance_diff_neg_max",
            "equity_balance_diff_neg_mean",
            "equity_balance_diff_pos_max",
            "equity_balance_diff_pos_mean",
            "equity_choppiness",
            "equity_choppiness_w",
            "equity_jerkiness",
            "equity_jerkiness_w",
            "expected_shortfall_1pct",
            "exponential_fit_error",
            "exponential_fit_error_w",
            "gain",
            "gain_per_exposure_long",
            "gain_per_exposure_short",
            "loss_profit_ratio",
            "loss_profit_ratio_w",
            "mdg",
            "mdg_per_exposure_long",
            "mdg_per_exposure_short",
            "mdg_w",
            "mdg_w_per_exposure_long",
            "mdg_w_per_exposure_short",
            "omega_ratio",
            "omega_ratio_w",
            "sharpe_ratio",
            "sharpe_ratio_w",
            "sortino_ratio",
            "sortino_ratio_w",
            "sterling_ratio",
            "sterling_ratio_w",
        ]
        # Edit limits
        if self.config.optimize.limits:
            limits = True
        else:
            limits = False
        with st.expander("Edit Limits", expanded=limits):
            # Init
            if not "ed_key" in st.session_state:
                st.session_state.ed_key = 0
            ed_key = st.session_state.ed_key
            if f'select_limits_{ed_key}' in st.session_state:
                ed = st.session_state[f'select_limits_{ed_key}']
                for row in ed["edited_rows"]:
                    if "edit" in ed["edited_rows"][row]:
                        if ed["edited_rows"][row]["edit"]:
                            st.session_state.edit_limits = st.session_state.limits_data[row]["limit"]
                    if "delete" in ed["edited_rows"][row]:
                        if ed["edited_rows"][row]["delete"]:
                            self.config.optimize.limits.pop(st.session_state.limits_data[row]["limit"])
                            self.clean_limits_session_state()
                            st.rerun()
            if not "limits_data" in st.session_state:
                limits_data = []
                if self.config.optimize.limits:
                    for limit, value in self.config.optimize.limits.items():
                        limits_data.append({
                            "limit": limit,
                            "value": value,
                            "edit": False,
                            "delete": False
                        })
                st.session_state.limits_data = limits_data
            # Display limits
            if st.session_state.limits_data and not "edit_limits" in st.session_state:
                d = st.session_state.limits_data
                st.data_editor(data=d, height=36+(len(d))*35, key=f'select_limits_{ed_key}', disabled=['limit','value'])
            if "edit_opt_v7_add_limits_greater" in st.session_state:
                if st.session_state.edit_opt_v7_add_limits_greater_button:
                    self.config.optimize.limits[f'penalize_if_greater_than_{st.session_state.edit_opt_v7_add_limits_greater}'] = st.session_state.edit_opt_v7_add_limits_greater_value
                    self.clean_limits_session_state()
                    st.rerun()
            if "edit_opt_v7_add_limits_lower" in st.session_state:
                if st.session_state.edit_opt_v7_add_limits_lower_button:
                    self.config.optimize.limits[f'penalize_if_lower_than_{st.session_state.edit_opt_v7_add_limits_lower}'] = st.session_state.edit_opt_v7_add_limits_lower_value
                    self.clean_limits_session_state()
                    st.rerun()
            if "edit_opt_v7_add_limits_greater_btc" in st.session_state:
                if st.session_state.edit_opt_v7_add_limits_greater_btc_button:
                    self.config.optimize.limits[f'penalize_if_greater_than_btc_{st.session_state.edit_opt_v7_add_limits_greater_btc}'] = st.session_state.edit_opt_v7_add_limits_greater_value_btc
                    self.clean_limits_session_state()
                    st.rerun()
            if "edit_opt_v7_add_limits_lower_btc" in st.session_state:
                if st.session_state.edit_opt_v7_add_limits_lower_btc_button:
                    self.config.optimize.limits[f'penalize_if_lower_than_btc_{st.session_state.edit_opt_v7_add_limits_lower_btc}'] = st.session_state.edit_opt_v7_add_limits_lower_value_btc
                    self.clean_limits_session_state()
                    st.rerun()
            if "edit_limits" in st.session_state:
                if st.session_state.edit_limits in self.config.optimize.limits:
                    self.edit_limits(st.session_state.edit_limits, self.config.optimize.limits[st.session_state.edit_limits])
                else:
                    self.edit_limits(st.session_state.edit_limits)
            else:
                col1, col2, col3, col4, col5, col6 = st.columns([1,0.5,0.5,1,0.5,0.5], vertical_alignment="bottom")
                with col1:
                    st.selectbox('penalize_if_greater_than', LIMITS, key="edit_opt_v7_add_limits_greater", help=pbgui_help.limits)
                with col2:
                    st.number_input("limit", format="%.5f", key="edit_opt_v7_add_limits_greater_value")
                with col3:
                    st.button("Add Limit", key="edit_opt_v7_add_limits_greater_button")
                with col4:
                    st.selectbox('penalize_if_lower_than', LIMITS, key="edit_opt_v7_add_limits_lower", help=pbgui_help.limits)
                with col5:
                    st.number_input("limit", format="%.5f", key="edit_opt_v7_add_limits_lower_value")
                with col6:
                    st.button("Add Limit", key="edit_opt_v7_add_limits_lower_button")
                # btc
                col1, col2, col3, col4, col5, col6 = st.columns([1,0.5,0.5,1,0.5,0.5], vertical_alignment="bottom")
                with col1:
                    st.selectbox('penalize_if_greater_than_btc', LIMITS_BTC, key="edit_opt_v7_add_limits_greater_btc", help=pbgui_help.limits)
                with col2:
                    st.number_input("limit", format="%.5f", key="edit_opt_v7_add_limits_greater_value_btc")
                with col3:
                    st.button("Add Limit", key="edit_opt_v7_add_limits_greater_btc_button")
                with col4:
                    st.selectbox('penalize_if_lower_than_btc', LIMITS_BTC, key="edit_opt_v7_add_limits_lower_btc", help=pbgui_help.limits)
                with col5:
                    st.number_input("limit", format="%.5f", key="edit_opt_v7_add_limits_lower_value_btc")
                with col6:
                    st.button("Add Limit", key="edit_opt_v7_add_limits_lower_btc_button")


    def edit(self):
        # Init coindata
        if "coindata_bybit" not in st.session_state:
            st.session_state.coindata_bybit = CoinData()
            st.session_state.coindata_bybit.exchange = "bybit"
        if "coindata_binance" not in st.session_state:
            st.session_state.coindata_binance = CoinData()
            st.session_state.coindata_binance.exchange = "binance"
        if "coindata_gateio" not in st.session_state:
            st.session_state.coindata_gateio = CoinData()
            st.session_state.coindata_gateio.exchange = "gateio"
        if "coindata_bitget" not in st.session_state:
            st.session_state.coindata_bitget = CoinData()
            st.session_state.coindata_bitget.exchange = "bitget"
        # Display Editor
        col1, col2, col3, col4, col5 = st.columns([1,1,0.5,0.5,1])
        with col1:
            self.fragment_exchanges()
        with col2:
            self.fragment_name()
        with col3:
            self.fragment_start_date()
        with col4:
            self.fragment_end_date()
        with col5:
            self.fragment_logging()
        col1, col2, col3, col4, col5, col6 = st.columns([1,1,1,1,1,1], vertical_alignment="bottom")
        with col1:
            self.fragment_starting_balance()
        with col2:
            self.fragment_balance_sample_divider()
        with col3:
            self.fragment_btc_collateral_cap()
        with col4:
            self.fragment_btc_collateral_ltv_cap()
        with col5:
            self.fragment_filter_by_min_effective_cost_bt()
        with col6:
            self.fragment_iters()
        col1, col2, col3, col4, col5 = st.columns([1,1,1,1,1], vertical_alignment="bottom")
        with col1:
            self.fragment_n_cpus()
        with col2:
            self.fragment_starting_config()
        with col3:
            self.fragment_combine_ohlcvs()
        with col4:
            self.fragment_compress_results_file()
        with col5:
            self.fragment_write_all_results()
        with st.expander("Edit Config", expanded=False):
            self.config.bot.edit()

        # Limits
        self.fragment_limits()
        
        # Optimizer
        col1, col2, col3, col4 = st.columns([1,1,1,1])
        with col1:
            self.fragment_population_size()
        with col2:
            self.fragment_offspring_multiplier()
        with col3:
            self.fragment_crossover_probability()
        with col4:
            self.fragment_crossover_eta()
        col1, col2, col3, col4 = st.columns([1,1,1,1])
        with col1:
            self.fragment_mutation_probability()
        with col2:
            self.fragment_mutation_eta()
        with col3:
            self.fragment_mutation_indpb()
        with col4:
            self.fragment_scoring()

        # Filters
        self.fragment_filter_coins()
        self.fragment_optimizer_guide()

        # Optimizer Bounds
        col1, col2 = st.columns([1,1])
        with col1:
            with st.container(border=True):
                st.write("Bounds long")
                # self.fragment_long_close_grid_markup_range()
                # self.fragment_long_close_grid_min_markup()
                self.fragment_long_close_grid_markup_end()
                self.fragment_long_close_grid_markup_start()
                self.fragment_long_close_grid_qty_pct()
                self.fragment_long_close_trailing_grid_ratio()
                self.fragment_long_close_trailing_qty_pct()
                self.fragment_long_close_trailing_retracement_pct()
                self.fragment_long_close_trailing_threshold_pct()
                self.fragment_long_ema_span_0()
                self.fragment_long_ema_span_1()
                self.fragment_long_entry_grid_double_down_factor()
                self.fragment_long_entry_grid_spacing_log_span_hours()
                self.fragment_long_entry_grid_spacing_log_weight()
                self.fragment_long_entry_grid_spacing_pct()
                self.fragment_long_entry_grid_spacing_we_weight()
                self.fragment_long_entry_initial_ema_dist()
                self.fragment_long_entry_initial_qty_pct()
                self.fragment_long_entry_trailing_double_down_factor()
                self.fragment_long_entry_trailing_grid_ratio()
                self.fragment_long_entry_trailing_retracement_pct()
                self.fragment_long_entry_trailing_threshold_pct()
                self.fragment_long_filter_log_range_ema_span()
                self.fragment_long_filter_volume_drop_pct()
                self.fragment_long_filter_volume_ema_span()
                self.fragment_long_n_positions()
                self.fragment_long_total_wallet_exposure_limit()
                self.fragment_long_unstuck_close_pct()
                self.fragment_long_unstuck_ema_dist()
                self.fragment_long_unstuck_loss_allowance_pct()
                self.fragment_long_unstuck_threshold()

        with col2:
            with st.container(border=True):
                st.write("Bounds short")
                # self.fragment_short_close_grid_markup_range()
                # self.fragment_short_close_grid_min_markup()
                self.fragment_short_close_grid_markup_end()
                self.fragment_short_close_grid_markup_start()
                self.fragment_short_close_grid_qty_pct()
                self.fragment_short_close_trailing_grid_ratio()
                self.fragment_short_close_trailing_qty_pct()
                self.fragment_short_close_trailing_retracement_pct()
                self.fragment_short_close_trailing_threshold_pct()
                self.fragment_short_ema_span_0()
                self.fragment_short_ema_span_1()
                self.fragment_short_entry_grid_double_down_factor()
                self.fragment_short_entry_grid_spacing_log_span_hours()
                self.fragment_short_entry_grid_spacing_log_weight()
                self.fragment_short_entry_grid_spacing_pct()
                self.fragment_short_entry_grid_spacing_we_weight()
                self.fragment_short_entry_initial_ema_dist()
                self.fragment_short_entry_initial_qty_pct()
                self.fragment_short_entry_trailing_double_down_factor()
                self.fragment_short_entry_trailing_grid_ratio()
                self.fragment_short_entry_trailing_retracement_pct()
                self.fragment_short_entry_trailing_threshold_pct()
                self.fragment_short_filter_log_range_ema_span()
                self.fragment_short_filter_volume_drop_pct()
                self.fragment_short_filter_volume_ema_span()
                self.fragment_short_n_positions()
                self.fragment_short_total_wallet_exposure_limit()
                self.fragment_short_unstuck_close_pct()
                self.fragment_short_unstuck_ema_dist()
                self.fragment_short_unstuck_loss_allowance_pct()
                self.fragment_short_unstuck_threshold()

    def edit_limits(self, limit, value = 0.0):
        value = float(value)
        st.number_input(limit, value=value, format="%.5f", key="edit_opt_v7_limits", help=pbgui_help.limits)
        col1, col2, col3, col4, col5 = st.columns([1,1,1,1,1], vertical_alignment="bottom")
        with col1:
            if st.button("Save"):
                self.config.optimize.limits[limit] = st.session_state.edit_opt_v7_limits
                self.clean_limits_session_state()
                st.rerun()
        with col2:
            if st.button("Cancel"):
                self.clean_limits_session_state()
                st.rerun()
        with col3:
            if st.button("Delete"):
                self.config.optimize.limits.pop(limit)
                self.clean_limits_session_state()
                st.rerun()

    def clean_limits_session_state(self):
        if "edit_opt_v7_limits" in st.session_state:
            del st.session_state.edit_opt_v7_limits
        if "limits_data" in st.session_state:
            del st.session_state.limits_data
        if "ed_key" in st.session_state:
            del st.session_state.ed_key
        if "edit_limits" in st.session_state:
            del st.session_state.edit_limits
        if "edit_opt_v7_add_limits_greater_button" in st.session_state:
            del st.session_state.edit_opt_v7_add_limits_greater_button
        if "edit_opt_v7_add_limits_greater_value" in st.session_state:
            del st.session_state.edit_opt_v7_add_limits_greater_value
        if "edit_opt_v7_add_limits_lower_button" in st.session_state:
            del st.session_state.edit_opt_v7_add_limits_lower_button
        if "edit_opt_v7_add_limits_lower_value" in st.session_state:
            del st.session_state.edit_opt_v7_add_limits_lower_value
        if "edit_opt_v7_add_limits_greater_btc_button" in st.session_state:
            del st.session_state.edit_opt_v7_add_limits_greater_btc_button
        if "edit_opt_v7_add_limits_greater_value_btc" in st.session_state:
            del st.session_state.edit_opt_v7_add_limits_greater_value_btc
        if "edit_opt_v7_add_limits_lower_btc_button" in st.session_state:
            del st.session_state.edit_opt_v7_add_limits_lower_btc_button
        if "edit_opt_v7_add_limits_lower_value_btc" in st.session_state:
            del st.session_state.edit_opt_v7_add_limits_lower_value_btc

    def save(self):
        self.path = Path(f'{PBGDIR}/data/opt_v7')
        if not self.path.exists():
            self.path.mkdir(parents=True)
        
        # Prevent creating directories with / in the name
        self.name = self.name.replace("/", "_")
        
        self.config.config_file = Path(f'{self.path}/{self.name}.json')
        self.config.save_config()

    def save_queue(self):
        dest = Path(f'{PBGDIR}/data/opt_v7_queue')
        unique_filename = str(uuid.uuid4())
        file = Path(f'{dest}/{unique_filename}.json') 
        opt_dict = {
            "name": self.name,
            "filename": unique_filename,
            "json": str(self.config.config_file),
            "exchange": self.config.backtest.exchanges
        }
        dest.mkdir(parents=True, exist_ok=True)
        with open(file, "w", encoding='utf-8') as f:
            json.dump(opt_dict, f, indent=4)

    def remove(self):
        Path(self.config.config_file).unlink(missing_ok=True)

class OptimizesV7:
    def __init__(self):
        self.optimizes = []
        self.d = []
        self.sort = "Time"
        self.sort_order = True
        self.load_sort()

    def view_optimizes(self):
        # Init
        if not self.optimizes:
            self.find_optimizes()
        if not "ed_key" in st.session_state:
            st.session_state.ed_key = 0
        ed_key = st.session_state.ed_key
        if not self.d:
            for id, opt in enumerate(self.optimizes):
                self.d.append({
                    'id': id,
                    'Select': False,
                    'Name': opt.name,
                    'Time': opt.time,
                    'Exchange': opt.config.backtest.exchanges,
                    'BT Count': opt.backtest_count,
                    'item': opt,
                })
        column_config = {
            "id": None,
            'item': None,
            "Select": st.column_config.CheckboxColumn(label="Select"),
            "Time": st.column_config.DatetimeColumn(label="Time", format="YYYY-MM-DD HH:mm:ss"),
            }
        # Display optimizes
        if "sort_opt_v7" in st.session_state:
            if st.session_state.sort_opt_v7 != self.sort:
                self.sort = st.session_state.sort_opt_v7
                self.save_sort()
        else:
            st.session_state.sort_opt_v7 = self.sort
        if "sort_opt_v7_order" in st.session_state:
            if st.session_state.sort_opt_v7_order != self.sort_order:
                self.sort_order = st.session_state.sort_opt_v7_order
                self.save_sort()
        else:
            st.session_state.sort_opt_v7_order = self.sort_order
        # Display sort options
        col1, col2 = st.columns([1, 9], vertical_alignment="bottom")
        with col1:
            st.selectbox("Sort by:", ['Time', 'Name', 'BT Count', 'Exchange'], key=f'sort_opt_v7', index=0)
        with col2:
            st.checkbox("Reverse", value=True, key=f'sort_opt_v7_order')
        self.d = sorted(self.d, key=lambda x: x[st.session_state[f'sort_opt_v7']], reverse=st.session_state[f'sort_opt_v7_order'])
        height = 36+(len(self.d))*35
        if height > 1000: height = 1016
        st.data_editor(data=self.d, height=height, key=f'select_optimize_v7_{ed_key}', hide_index=None, column_order=None, column_config=column_config, disabled=['id','name'])

    def load_sort(self):
        pb_config = configparser.ConfigParser()
        pb_config.read('pbgui.ini')
        self.sort = pb_config.get("optimize_v7", "sort") if pb_config.has_option("optimize_v7", "sort") else "Time"
        self.sort_order = eval(pb_config.get("optimize_v7", "sort_order")) if pb_config.has_option("optimize_v7", "sort_order") else True

    def save_sort(self):
        pb_config = configparser.ConfigParser()
        pb_config.read('pbgui.ini')
        if not pb_config.has_section("optimize_v7"):
            pb_config.add_section("optimize_v7")
        pb_config.set("optimize_v7", "sort", str(self.sort))
        pb_config.set("optimize_v7", "sort_order", str(self.sort_order))
        with open('pbgui.ini', 'w') as f:
            pb_config.write(f)

    def find_optimizes(self):
        p = str(Path(f'{PBGDIR}/data/opt_v7/*.json'))
        found_opt = glob.glob(p, recursive=False)
        if found_opt:
            for p in found_opt:
                opt = OptimizeV7Item(p)
                self.optimizes.append(opt)

    def edit_selected(self):
        ed_key = st.session_state.ed_key
        ed = st.session_state[f'select_optimize_v7_{ed_key}']
        # Get number of selected results
        selected_count = sum(1 for row in ed["edited_rows"] if "Select" in ed["edited_rows"][row] and ed["edited_rows"][row]["Select"])
        if selected_count == 0:
            error_popup("No Optimizes selected")
            return
        elif selected_count > 1:
            error_popup("Please select only one Optimize to view")
            return
        for row in ed["edited_rows"]:
            if "Select" in ed["edited_rows"][row]:
                if ed["edited_rows"][row]["Select"]:
                    st.session_state.opt_v7 = self.d[row]["item"]
                    st.rerun()

    @st.dialog("No Optimize selected. Delete all?")
    def remove_all(self):
        st.warning(f"Delete all Optimizes?", icon="⚠️")
        col1, col2 = st.columns([1,1])
        with col1:
            if st.button(":green[Yes]"):
                shutil.rmtree(f'{PBGDIR}/data/opt_v7', ignore_errors=True)
                self.d = []
                self.optimizes = []
                st.rerun()
        with col2:
            if st.button(":red[No]"):
                st.rerun()

    def remove_selected(self):
        ed_key = st.session_state.ed_key
        ed = st.session_state[f'select_optimize_v7_{ed_key}']
        # Get number of selected results
        selected_count = sum(1 for row in ed["edited_rows"] if "Select" in ed["edited_rows"][row] and ed["edited_rows"][row]["Select"])
        if selected_count == 0:
            self.remove_all()
            return
        for row in ed["edited_rows"]:
            if "Select" in ed["edited_rows"][row]:
                if ed["edited_rows"][row]["Select"]:
                    self.d[row]["item"].remove()
        self.d = []
        self.optimizes = []
        st.rerun()


def main():
    # Disable Streamlit Warnings when running directly
    logging.getLogger("streamlit.runtime.state.session_state_proxy").disabled=True
    logging.getLogger("streamlit.runtime.scriptrunner_utils.script_run_context").disabled=True
    opt = OptimizeV7Queue()
    while True:
        opt.load()
        for item in opt.items:
            while opt.running():
                time.sleep(5)
            pb_config = configparser.ConfigParser()
            pb_config.read('pbgui.ini')
            if not eval(pb_config.get("optimize_v7", "autostart")):
                return
            if item.is_existing():
                if item.status() == "not started" or item.status() == "error":
                    print(f'{datetime.datetime.now().isoformat(sep=" ", timespec="seconds")} Optimizing {item.filename} started')
                    item.run()
                    time.sleep(1)
            else:
                print(f'{datetime.datetime.now().isoformat(sep=" ", timespec="seconds")} Optimize config file for {item.filename} not found, jumping to next in queue')
        time.sleep(60)

if __name__ == '__main__':
    main()
