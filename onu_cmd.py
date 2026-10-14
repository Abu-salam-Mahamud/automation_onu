import sys
import subprocess
import tkinter as tk
from tkinter import messagebox

# =====================================================================
#  AUTO-INSTALLER LOGIC
# =====================================================================
def install_and_import(package_name, import_name=None):
    if import_name is None:
        import_name = package_name
    try:
        __import__(import_name)
    except ImportError:
        root = tk.Tk()
        root.withdraw()
        msg = f"'{package_name}' library ti pawa jacche na!\n\nScript ekhon auto-install korbe. Please wait..."
        print(msg)
        messagebox.showinfo("Installing Package", msg)
        try:
            subprocess.check_call([sys.executable, "-m", "pip", "install", package_name])
            messagebox.showinfo("Success", f"'{package_name}' successfully installed!\nScript ekhon run hobe.")
            root.destroy()
        except subprocess.CalledProcessError:
            messagebox.showerror("Error", f"'{package_name}' install korte shomossha hoyeche!\nDoya kore internet connection check korun.")
            root.destroy()
            sys.exit(1)

install_and_import('netmiko')
install_and_import('routeros_api')

import re
import time
import json
import os
import threading
from tkinter import ttk, scrolledtext
from netmiko.huawei.huawei import HuaweiTelnet
import routeros_api

# =====================================================================
#  GLOBAL VARIABLES & VLAN DATABASE
# =====================================================================
OLT_DEVICE = {}
CURRENT_IP = ""
OLT_NAME = ""

# File to store all configs per IP
DB_FILE = "olt_config_db.json"
olt_db = {}

def load_db():
    global olt_db
    if os.path.exists(DB_FILE):
        with open(DB_FILE, 'r') as f:
            try:
                olt_db = json.load(f)
            except:
                olt_db = {}
    
    needs_save = False
    
    # Default config for old SNS OLT 
    if "172.31.31.250" not in olt_db:
        olt_db["172.31.31.250"] = {
            "0/1": {"start": 101, "end": 251, "inc": 10, "line_profile": 100, "srv_profile": 100},
            "0/2": {"start": 259, "end": 274, "inc": 1, "line_profile": 100, "srv_profile": 100},
            "0/3": {"start": 280, "end": 295, "inc": 1, "line_profile": 100, "srv_profile": 100},
            "0/4": {"start": 400, "end": 415, "inc": 1, "line_profile": 100, "srv_profile": 100}
        }
        needs_save = True

    # Default config for Habibnagar OLT
    if "172.31.31.226" not in olt_db:
        olt_db["172.31.31.226"] = {
            "0/0": {"start": 2120, "end": 2135, "inc": 1, "line_profile": 10, "srv_profile": 10},
            "0/1": {"start": 2150, "end": 2165, "inc": 1, "line_profile": 10, "srv_profile": 10}
        }
        needs_save = True

    # Default config for Kusabo OLT
    if "172.31.31.62" not in olt_db:
        olt_db["172.31.31.62"] = {
            "0/0": {"start": 2000, "end": 2015, "inc": 1, "line_profile": 10, "srv_profile": 10}
        }
        needs_save = True

    if needs_save:
        save_db()

def save_db():
    with open(DB_FILE, 'w') as f:
        json.dump(olt_db, f, indent=4)

load_db()


# =====================================================================
#  OLT CONNECTION / CORE LOGIC
# =====================================================================
class HuaweiOLTTelnet(HuaweiTelnet):
    def session_preparation(self):
        self._test_channel_read(pattern=r"[>#]")
        self.set_base_prompt()


PROMPT_RE = re.compile(r"(?:^|\n)[\w\-.]+(?:\([\w\-/.]+\))?[>#]\s*$")


def run(conn, cmd, timeout=10):
    """Command pathao, prompt ashle sathe sathe return koro."""
    conn.write_channel(cmd + "\n")
    out = ""
    start = time.time()
    while time.time() - start < timeout:
        time.sleep(0.05)
        chunk = conn.read_channel()
        if not chunk:
            continue
        out += chunk
        tail = out[-200:]
        if "---- More" in tail[-60:]:
            conn.write_channel(" ")
        elif "(y/n)" in tail[-40:]:
            conn.write_channel("y\n")
        elif re.search(r"\{\s*<cr>.*\}\s*:\s*$", tail):
            conn.write_channel("\n")
        elif len(out) > len(cmd) and PROMPT_RE.search(out):
            break
    return out


def get_vlan(slot_str, pon_num):
    if CURRENT_IP in olt_db and slot_str in olt_db[CURRENT_IP]:
        cfg = olt_db[CURRENT_IP][slot_str]
        return cfg["start"] + (pon_num * cfg["inc"])
    return 0

def get_profiles(slot_str):
    """Returns (line_profile, srv_profile) from db for current IP and slot"""
    if CURRENT_IP in olt_db and slot_str in olt_db[CURRENT_IP]:
        cfg = olt_db[CURRENT_IP][slot_str]
        return cfg.get("line_profile", 100), cfg.get("srv_profile", 100)
    return 100, 100


def open_olt(log):
    t0 = time.time()
    log(f"Connecting to {OLT_NAME} ({CURRENT_IP}) via Telnet...")
    conn = HuaweiOLTTelnet(**OLT_DEVICE)
    log(f"Connected in {time.time() - t0:.1f}s", "ok")
    run(conn, "enable")
    run(conn, "scroll 512")
    run(conn, "config")
    return conn


def find_ont(conn, sn):
    out = run(conn, f"display ont info by-sn {sn}")
    fsp = re.search(r'F/S/P\s*:\s*(\d+)/(\d+)/(\d+)', out)
    oid = re.search(r'ONT-ID\s*:\s*(\d+)', out)
    if not fsp or not oid:
        return None, out
    frame, slot_no, pon_id = fsp.groups()
    return (f"{frame}/{slot_no}", int(pon_id), oid.group(1)), out


def remove_ont(conn, sn, log):
    loc, out = find_ont(conn, sn)
    if not loc:
        return None, out
    slot, pon, onu = loc
    log(f"-> Found: slot {slot}, PON {pon}, ONU ID {onu}", "ok")

    srv_out = run(conn, f"display service-port port {slot}/{pon} ont {onu}")
    srv_indexes = re.findall(r'^\s*(\d+)\s+\d+\s+\S+\s+gpon\b', srv_out, re.MULTILINE)
    if srv_indexes:
        log(f"-> Service ports: {srv_indexes}")
        for idx in srv_indexes:
            run(conn, f"undo service-port {idx}")
            log(f"-> Service port {idx} deleted.")
    else:
        log("-> No service port found.", "dim")

    run(conn, f"interface gpon {slot}")
    run(conn, f"ont delete {pon} {onu}")
    run(conn, "quit")
    log(f"-> ONU {onu} deleted from {slot} PON {pon}.", "ok")
    return loc, out


def provision_pon(conn, slot, pon, log):
    vlan = get_vlan(slot, pon)
    line_prof, srv_prof = get_profiles(slot)
    
    run(conn, f"interface gpon {slot}")
    out = run(conn, f"ont confirm {pon} all sn-auth omci "
                    f"ont-lineprofile-id {line_prof} ont-srvprofile-id {srv_prof}")
    run(conn, "quit")
    onu_ids = sorted(set(re.findall(r'(?:ONT[-_ ]?ID)\s*[:=]?\s*(\d+)', out, re.IGNORECASE)), key=int)
    done = []
    for onu in onu_ids:
        run(conn, f"service-port vlan {vlan} gpon {slot}/{pon} ont {onu} eth 1 "
                  f"multi-service user-vlan untagged tag-transform default")
        log(f"Service Port Done -> Slot {slot}, PON {pon}, ONU {onu}, VLAN {vlan}", "ok")
        done.append((onu, vlan))
    return done


def save_config(conn, log):
    log("\nSaving OLT configuration. Please wait...", "head")
    run(conn, "save", timeout=30)
    log("Configuration saved successfully!", "ok")


# ---------- Job 1: Scan & Add ----------
def job_scan_and_add(ui):
    t0 = time.time()
    conn = None
    try:
        conn = open_olt(ui.log)
        step = 0
        
        slots_to_scan = sorted(olt_db.get(CURRENT_IP, {}).keys())
        if not slots_to_scan:
            ui.log("Kono Slot/VLAN configure kora nai! Age 'OLT Config' tab theke add korun.", "warn")
            return None

        for slot in slots_to_scan:
            tasks = []
            run(conn, f"interface gpon {slot}")
            ui.log(f"\n--- Scanning all PONs (0-15) in Slot {slot} ---", "head")
            ui.status(f"Scanning Slot {slot}...")

            for pon in range(16):
                vlan_id = get_vlan(slot, pon)
                line_prof, srv_prof = get_profiles(slot)
                
                out = run(conn, f"ont confirm {pon} all sn-auth omci "
                                f"ont-lineprofile-id {line_prof} ont-srvprofile-id {srv_prof}")

                low = out.lower()
                if "success: 0" in low or "failure" in low or "already" in low:
                    chk = run(conn, f"display ont autofind {pon}")
                    for mac in re.findall(r'Ont SN\s*:\s*([0-9A-F]{16})', chk, re.IGNORECASE):
                        mac = mac.upper()
                        ui.log(f"[!] ALERT: {mac} (Slot {slot} PON {pon}) age thekei OLT er onno kothao config kora ase!", "warn")
                        ui.warn_row(slot, pon, mac)

                onu_ids = list(set(re.findall(r'(?:ONT[-_ ]?ID)\s*[:=]?\s*(\d+)', out, re.IGNORECASE)))
                if onu_ids:
                    ui.log(f"Slot {slot} PON {pon}: ONU found {onu_ids}, VLAN {vlan_id}", "ok")
                    for onu_id in onu_ids:
                        tasks.append({'pon': pon, 'onu': onu_id, 'vlan': vlan_id})
                else:
                    ui.log(f"Slot {slot} PON {pon}: none", "dim")

                step += 1
                ui.progress(step)

            run(conn, "quit")
            if tasks:
                ui.log(f"\nCreating Service Ports for Slot {slot}...", "head")
                for t in tasks:
                    run(conn, f"service-port vlan {t['vlan']} gpon {slot}/{t['pon']} "
                              f"ont {t['onu']} eth 1 multi-service user-vlan untagged "
                              f"tag-transform default")
                    ui.log(f"Service Port Done -> Slot {slot}, PON {t['pon']}, ONU {t['onu']}, VLAN {t['vlan']}", "ok")
                    ui.add_row(slot, t['pon'], t['onu'], t['vlan'])
            else:
                ui.log(f"No new ONUs found in Slot {slot}.", "dim")

        ui.status("Saving configuration...")
        save_config(conn, ui.log)
        ui.log(f"\nTotal time: {time.time() - t0:.1f}s", "dim")
        return None
    except Exception as e:
        ui.log(f"An error occurred: {e}", "warn")
        return str(e)
    finally:
        if conn:
            try: conn.disconnect()
            except: pass


# ---------- Job 2: Delete Single ONU ----------
def job_delete(ui, sn):
    t0 = time.time()
    conn = None
    try:
        conn = open_olt(ui.log)
        ui.log(f"\nSearching ONU {sn} ...", "head")
        loc, out = remove_ont(conn, sn, ui.log)
        if not loc:
            ui.log(f"ONU with SN {sn} not found on OLT!", "warn")
            ui.log(out, "dim")
            return ("notfound", None)
        save_config(conn, ui.log)
        ui.log(f"Total time: {time.time() - t0:.1f}s", "dim")
        return ("ok", loc)
    except Exception as e:
        ui.log(f"An error occurred: {e}", "warn")
        return ("error", str(e))
    finally:
        if conn:
            try: conn.disconnect()
            except: pass


# ---------- Job 2.1: Bulk Delete PON ----------
def job_bulk_delete(ui, slot, pon):
    t0 = time.time()
    conn = None
    try:
        conn = open_olt(ui.log)
        ui.log(f"\n--- BULK DELETE: Slot {slot} PON {pon} er sob ONU ---", "warn")
        
        ui.log("1. Removing all service ports for this PON...", "head")
        out1 = run(conn, f"undo service-port port {slot}/{pon}", timeout=25)
        ui.log(out1.strip() or "Done.", "dim")
        
        ui.log(f"2. Entering interface gpon {slot} and deleting ALL ONUs...", "head")
        run(conn, f"interface gpon {slot}")
        out2 = run(conn, f"ont delete {pon} all", timeout=35)
        ui.log(out2.strip() or "All ONUs deleted.", "ok")
        
        run(conn, "quit")
        save_config(conn, ui.log)
        
        ui.log(f"Bulk Delete complete in {time.time() - t0:.1f}s", "ok")
        return "ok"
    except Exception as e:
        ui.log(f"An error occurred: {e}", "warn")
        return "error"
    finally:
        if conn:
            try: conn.disconnect()
            except: pass


# ---------- Job 3: Conflict Fix ----------
def job_conflict_fix(ui):
    t0 = time.time()
    conn = None
    try:
        conn = open_olt(ui.log)
        ui.log("\nReading autofind list...", "head")
        out = run(conn, "display ont autofind all", timeout=30)
        found = re.findall(r'F/S/P\s*:\s*(\d+)/(\d+)/(\d+).*?Ont SN\s*:\s*([0-9A-F]{16})',
                           out, re.IGNORECASE | re.DOTALL)
        seen, items = set(), []
        for f, s, p, sn in found:
            sn = sn.upper()
            if sn not in seen:
                seen.add(sn)
                items.append((f"{f}/{s}", int(p), sn))

        if not items:
            ui.log("Autofind e kono ONU nai.", "dim")
            return (0, 0)

        ui.log(f"{len(items)} ta ONU autofind e pawa gese. Conflict check hocche...", "head")
        pons = {}
        fixed_sns = []
        for i, (slot, pon, sn) in enumerate(items, 1):
            ui.status(f"Checking {i}/{len(items)}")
            ui.log(f"\n[{i}/{len(items)}] {sn} (autofind: {slot} PON {pon})")
            loc, _ = remove_ont(conn, sn, ui.log)
            if not loc:
                ui.log("   Onno kothao config nai -> conflict noy (Auto Add tab e jabe).", "dim")
                continue
            old = f"{loc[0]} PON {loc[1]} ONU {loc[2]}"
            ui.conflict_row(sn, old, "Old config deleted")
            pons.setdefault((slot, pon), []).append(sn)
            fixed_sns.append(sn)

        if not pons:
            ui.log("\nKono conflict paoa jayni.", "dim")
            return (0, len(items))

        ui.log("\nRe-configuring cleaned PONs...", "head")
        ok = 0
        for (slot, pon), sns in pons.items():
            done = provision_pon(conn, slot, pon, ui.log)
            for sn in sns:
                if done:
                    ids = ",".join(o for o, _ in done)
                    ui.conflict_update(sn, f"{slot} PON {pon} ONU {ids}", get_vlan(slot, pon), "Fixed")
                    ok += 1
                else:
                    ui.conflict_update(sn, "-", "-", "Delete hoyeche, config hoyni")

        save_config(conn, ui.log)
        ui.log(f"\nTotal time: {time.time() - t0:.1f}s", "dim")
        return (ok, len(fixed_sns))
    except Exception as e:
        ui.log(f"An error occurred: {e}", "warn")
        return str(e)
    finally:
        if conn:
            try: conn.disconnect()
            except: pass


# ---------- Job 3.1: 4-Port ONU Config ----------
def job_4port_config(ui, sn):
    t0 = time.time()
    conn = None
    try:
        conn = open_olt(ui.log)
        ui.log(f"\n--- 4-Port ONU Setup for {sn} ---", "head")
        
        # 1. Delete if already configured
        loc, _ = remove_ont(conn, sn, ui.log)
        if loc:
            ui.log("Old config deleted. Waiting 5s for ONU to appear in autofind...", "dim")
            time.sleep(5)
        
        # 2. Find in autofind
        ui.log("Searching autofind for the ONU...", "head")
        out = run(conn, "display ont autofind all", timeout=30)
        match = re.search(rf'F/S/P\s*:\s*(\d+)/(\d+)/(\d+).*?Ont SN\s*:\s*({sn})', out, re.IGNORECASE | re.DOTALL)
        
        if not match:
            ui.log(f"ONU {sn} autofind e pawa jayni. Cable connection check korun.", "warn")
            return ("error", "Not found in autofind")
            
        frame, slot_no, pon, found_sn = match.groups()
        slot = f"{frame}/{slot_no}"
        pon = int(pon)
        
        vlan = get_vlan(slot, pon)
        prof_id = vlan # User instruction: Line & Srv Profile = VLAN ID
        
        ui.log(f"Found in Slot {slot} PON {pon}. VLAN = {vlan}", "ok")
        
        # 3. Confirm 4-Port ONU (With Smart Retry Logic)
        ui.log(f"Entering interface gpon {slot}...", "dim")
        run(conn, f"interface gpon {slot}")
        
        cmd = f"ont confirm {pon} sn-auth {sn} omci ont-lineprofile-id {prof_id} ont-srvprofile-id {prof_id}"
        conf_out = run(conn, cmd)
        
        # OLT conflict retry logic
        if "conflict" in conf_out.lower() or "retry later" in conf_out.lower():
            ui.log("OLT busy ache (conflict). 5 second wait kore abar try kora hocche...", "warn")
            time.sleep(5)
            run(conn, "\n") # Send enter to clear
            conf_out = run(conn, cmd)
        
        onu_match = re.search(r'(?:ONT[-_ ]?ID)\s*[:=]?\s*(\d+)', conf_out, re.IGNORECASE)
        if not onu_match:
            ui.log("ONU Confirm korte byartho hoyeche! Output:", "warn")
            ui.log(conf_out, "dim")
            run(conn, "quit")
            return ("error", "Confirm Failed")
            
        onu_id = onu_match.group(1)
        ui.log(f"Confirmed as ONU ID {onu_id}.", "ok")
        
        # 4. Native VLAN on all 4 Ports
        ui.log(f"Applying Native VLAN {vlan} to all 4 ETH ports...", "head")
        for port in range(1, 5):
            run(conn, f"ont port native-vlan {pon} {onu_id} eth {port} vlan {vlan}")
            ui.log(f"-> ETH {port} configured.")
            
        run(conn, "quit")
        
        # 5. Service Port (4-Port ONU: gemport 0 + user-vlan = VLAN ID)
        ui.log("Creating 4-port service port (gemport 0)...", "head")
        srv_cmd = f"service-port vlan {vlan} gpon {slot}/{pon} ont {onu_id} gemport 0 multi-service user-vlan {vlan}"
        run(conn, srv_cmd)
        ui.log(f"Service port created -> VLAN {vlan}, gpon {slot}/{pon}, ONU {onu_id}, gemport 0.", "ok")
        
        save_config(conn, ui.log)
        ui.log(f"\n4-Port ONU configuration successful in {time.time() - t0:.1f}s", "ok")
        return ("ok", (slot, pon, onu_id, vlan))
        
    except Exception as e:
        ui.log(f"An error occurred: {e}", "warn")
        return ("error", str(e))
    finally:
        if conn:
            try: conn.disconnect()
            except: pass


# ---------- Job 4: Optical Info ----------
def detect_vlan_from_mikrotik(mk_conn, username, mac_address, log):
    """
    MikroTik theke user kon VLAN er under e ache ta ber kore.
    Return: VLAN ID (int) ba None.
    Method 1: PPPoE server session er interface/service name -> VLAN interface
    Method 2: Router MAC er bridge host table (on-interface) -> VLAN interface
    VLAN interface er 'vlan-id' use hobe; na pele nam er "-V-2132" ongsho theke nibe.
    """
    # VLAN interface list: name -> vlan-id
    vlan_map = {}
    try:
        for v in mk_conn.get_resource('/interface/vlan').get():
            nm = v.get('name', '')
            vid = str(v.get('vlan-id', '')).strip()
            if nm and vid.isdigit():
                vlan_map[nm] = int(vid)
    except Exception as e:
        log(f"VLAN interface list porte parini: {e}", "dim")

    def resolve(text):
        text = (text or "").strip()
        if not text:
            return None
        if text in vlan_map:
            return vlan_map[text]
        m = re.search(r'-V-(\d+)$', text, re.IGNORECASE)
        if m:
            return int(m.group(1))
        return None

    # Method 1: PPPoE server session
    try:
        sessions = mk_conn.get_resource('/interface/pppoe-server').get(user=username)
        for s in sessions:
            for key in ('interface', 'service', 'service-name'):
                vid = resolve(s.get(key))
                if vid:
                    log(f"-> MikroTik PPPoE session ({key}: {s.get(key)}) theke VLAN {vid} pawa gese.", "ok")
                    return vid
    except Exception as e:
        log(f"PPPoE server session check kora jayni: {e}", "dim")

    # Method 2: bridge host table (MAC kon interface e shikha hoyeche)
    try:
        hosts = mk_conn.get_resource('/interface/bridge/host').get(**{'mac-address': mac_address.upper()})
        for h in hosts:
            for key in ('on-interface', 'interface'):
                vid = resolve(h.get(key))
                if vid:
                    log(f"-> MikroTik bridge host ({key}: {h.get(key)}) theke VLAN {vid} pawa gese.", "ok")
                    return vid
    except Exception as e:
        log(f"Bridge host table check kora jayni: {e}", "dim")

    return None


def job_optical_info(ui, username):
    t0 = time.time()
    conn = None
    mk_pool = None
    try:
        if not routeros_api:
            err = "routeros_api package missing! CMD te likhun: pip install routeros_api"
            ui.log(err, "warn")
            return ("error", err)

        ui.log(f"\nConnecting to MikroTik to find user '{username}'...", "head")
        ui.status("Querying MikroTik...")
        
        mk_pool = routeros_api.RouterOsApiPool(
            '103.159.255.249', 
            username='Logserver', 
            password='Logserver*#@Logserver', 
            port=58738, 
            plaintext_login=True
        )
        mk_conn = mk_pool.get_api()
        
        active = mk_conn.get_resource('/ppp/active')
        users = active.get(name=username)
        
        if not users:
            ui.log(f"[!] User '{username}' MikroTik e active nei.", "warn")
            mk_pool.disconnect() 
            return ("error", "User is not active in MikroTik")
            
        mac_address = users[0].get('caller-id', '')
        
        if not mac_address:
            ui.log("[!] User active ache kintu 'caller-id' (MAC) pawa jayni.", "warn")
            mk_pool.disconnect() 
            return ("error", "No MAC found for user")
            
        ui.log(f"User found! Router MAC: {mac_address}", "ok")

        # ---- Auto detect VLAN from MikroTik ----
        ui.status("Detecting VLAN...")
        detected_vlan = detect_vlan_from_mikrotik(mk_conn, username, mac_address, ui.log)
        
        mk_pool.disconnect()
        mk_pool = None
        
        mac_clean = mac_address.replace(':', '').lower()
        if len(mac_clean) != 12:
            ui.log(f"[!] Invalid MAC format: {mac_address}", "warn")
            return ("error", "Invalid MAC format")
        
        hw_mac = f"{mac_clean[0:4]}-{mac_clean[4:8]}-{mac_clean[8:12]}"
        ui.log(f"Huawei Formatted MAC: {hw_mac}", "dim")

        # VLAN search list: MikroTik theke pawa VLAN age, tarpor fallback hisebe ei OLT er configured VLAN gulo
        vlan_candidates = []
        if detected_vlan:
            vlan_candidates.append(detected_vlan)
            ui.log(f"Detected VLAN: {detected_vlan}", "ok")
        else:
            ui.log("MikroTik theke VLAN ber kora jayni. OLT er configured VLAN gulo te khoja hobe...", "warn")
        for slot_key in sorted(olt_db.get(CURRENT_IP, {}).keys()):
            for p in range(16):
                v = get_vlan(slot_key, p)
                if v and v not in vlan_candidates:
                    vlan_candidates.append(v)

        if not vlan_candidates:
            ui.log("Kono VLAN pawa jayni. 'OLT Config' tab check korun.", "warn")
            return ("error", "VLAN not detected")
        
        ui.status("Connecting OLT...")
        conn = open_olt(ui.log)

        match = None
        vlan_id = None
        for idx, cand in enumerate(vlan_candidates):
            ui.log(f"Searching MAC in VLAN {cand}...", "head" if idx == 0 else "dim")
            mac_out = run(conn, f"display mac-address vlan {cand}", timeout=30)
            match = re.search(rf'{hw_mac}.*?(\d+)\s*/\s*(\d+)\s*/\s*(\d+)\s+(\d+)', mac_out, re.IGNORECASE)
            if match:
                vlan_id = cand
                break
        
        if not match:
            ui.log(f"MAC {hw_mac} OLT er kono VLAN MAC table e pawa jayni.", "warn")
            return ("error", "MAC not found in OLT")
        
        frame, slot_no, pon, onu = match.groups()
        slot = f"{frame}/{slot_no}"
            
        loc_str = f"Slot {slot} PON {pon} ONU {onu} (VLAN {vlan_id})"
        ui.log(f"-> MAC matched! OLT Location: {loc_str}", "ok")
        
        ui.root.after(0, lambda: ui.card_onu.config(text=f"{slot}/{pon}/{onu}"))
        ui.status("Reading Optical Info...")
        
        ui.log(f"Entering interface gpon {slot}...", "dim")
        run(conn, f"interface gpon {slot}")
        
        ui.log(f"Executing 'display ont optical-info {pon} {onu}'...", "head")
        opt_out = run(conn, f"display ont optical-info {pon} {onu}")
        
        run(conn, "quit")
        
        rx = re.search(r'Rx optical power\s*\(dBm\)\s*:\s*([-0-9.]+)', opt_out)
        tx = re.search(r'Tx optical power\s*\(dBm\)\s*:\s*([-0-9.]+)', opt_out)
        temp = re.search(r'Temperature\s*\(C\)\s*:\s*([-0-9.]+)', opt_out)
        
        rx_val = rx.group(1) if rx else "N/A"
        tx_val = tx.group(1) if tx else "N/A"
        temp_val = temp.group(1) if temp else "N/A"
        
        ui.log(f"Result: Rx={rx_val} dBm | Tx={tx_val} dBm | Temp={temp_val} °C", "ok")
        ui.log(f"Total time: {time.time() - t0:.1f}s", "dim")
        
        return ("ok", (rx_val, tx_val, temp_val, loc_str))
        
    except Exception as e:
        ui.log(f"An error occurred: {e}", "warn")
        return ("error", str(e))
    finally:
        if mk_pool:
            try: mk_pool.disconnect() 
            except: pass
        if conn:
            try: conn.disconnect()
            except: pass


# =====================================================================
#  GUI SYSTEM
# =====================================================================
BG = "#f3f5f9"
NAVY = "#1f2a44"
GREEN = "#2e9e5b"
RED = "#d64545"
AMBER = "#e8a317"
BLUE = "#2f6fdb"
GREY = "#6b7385"
GREY_BTN = "#9aa3b5"

class LoginDialog:
    def __init__(self, parent, on_success):
        self.top = tk.Toplevel(parent)
        self.top.title("OLT Login")
        self.top.geometry("420x460")
        self.top.configure(bg=NAVY)
        self.top.protocol("WM_DELETE_WINDOW", parent.destroy)
        self.on_success = on_success
        
        tk.Label(self.top, text="CONNECT TO OLT", bg=NAVY, fg="white", 
                 font=("Segoe UI", 18, "bold")).pack(pady=(30, 20))
        
        tk.Label(self.top, text="OLT IP Address", bg=NAVY, fg="#b9c2d9", font=("Segoe UI", 10)).pack(anchor="w", padx=40)
        self.ip_ent = tk.Entry(self.top, font=("Consolas", 14), relief="flat")
        self.ip_ent.pack(fill="x", padx=40, pady=(0, 15), ipady=6)
        self.ip_ent.insert(0, "172.31.31.250")
        
        tk.Label(self.top, text="Username", bg=NAVY, fg="#b9c2d9", font=("Segoe UI", 10)).pack(anchor="w", padx=40)
        self.user_ent = tk.Entry(self.top, font=("Consolas", 14), relief="flat")
        self.user_ent.pack(fill="x", padx=40, pady=(0, 15), ipady=6)
        self.user_ent.insert(0, "Mollah")
        
        tk.Label(self.top, text="Password", bg=NAVY, fg="#b9c2d9", font=("Segoe UI", 10)).pack(anchor="w", padx=40)
        self.pass_ent = tk.Entry(self.top, font=("Consolas", 14), relief="flat", show="*")
        self.pass_ent.pack(fill="x", padx=40, pady=(0, 25), ipady=6)
        self.pass_ent.insert(0, "sns#8998")
        
        self.btn = tk.Button(self.top, text="CONNECT", font=("Segoe UI", 12, "bold"), bg=GREEN, fg="white", 
                             activebackground="#25804a", activeforeground="white", relief="flat",
                             cursor="hand2", command=self.start_login)
        self.btn.pack(fill="x", padx=40, ipady=6)
        
        self.status = tk.Label(self.top, text="", bg=NAVY, fg=AMBER, font=("Segoe UI", 10))
        self.status.pack(pady=15)

    def start_login(self):
        ip = self.ip_ent.get().strip()
        user = self.user_ent.get().strip()
        pwd = self.pass_ent.get().strip()
        
        if not ip or not user or not pwd:
            self.status.config(text="Shob field puron korun!", fg=RED)
            return
            
        self.btn.config(state="disabled", text="CONNECTING...", bg=GREY_BTN)
        self.status.config(text="Please wait, verifying connection...", fg=AMBER)
        
        def worker():
            global OLT_DEVICE, CURRENT_IP, OLT_NAME
            temp_dev = {
                'device_type': 'huawei_telnet',
                'host': ip,
                'username': user,
                'password': pwd,
                'port': 23,
                'global_delay_factor': 1,
                'timeout': 15,
                'conn_timeout': 15,
            }
            try:
                conn = HuaweiOLTTelnet(**temp_dev)
                conn.write_channel("\n")
                time.sleep(0.5)
                prompt = conn.find_prompt()
                conn.disconnect()
                
                m = re.match(r"^([\w\-]+)", prompt.replace("\x1b[D", "").strip())
                name = m.group(1) if m else "OLT"
                
                OLT_DEVICE = temp_dev.copy()
                OLT_DEVICE['timeout'] = 60
                OLT_DEVICE['conn_timeout'] = 60
                CURRENT_IP = ip
                OLT_NAME = name
                
                if CURRENT_IP not in olt_db:
                    olt_db[CURRENT_IP] = {}
                    save_db()
                    
                self.top.after(0, self.success)
            except Exception as e:
                self.top.after(0, lambda err=str(e): self.fail(err))
                
        threading.Thread(target=worker, daemon=True).start()
        
    def success(self):
        self.top.destroy()
        self.on_success()
        
    def fail(self, err):
        self.btn.config(state="normal", text="CONNECT", bg=GREEN)
        self.status.config(text="Connection Failed! Check IP/Pass", fg=RED)
        messagebox.showerror("Login Error", err, parent=self.top)


class App:
    def __init__(self, root):
        self.root = root
        root.title(f"{OLT_NAME} Manager - {CURRENT_IP}")
        root.geometry("1020x800")
        root.minsize(820, 640)
        root.configure(bg=BG)

        self.running = False
        self.added_count = 0
        self.warn_count = 0
        self.total_steps = 0
        self.buttons = {}

        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("Treeview", rowheight=26, font=("Segoe UI", 10))
        style.configure("Treeview.Heading", font=("Segoe UI", 10, "bold"))
        style.configure("TNotebook.Tab", font=("Segoe UI", 10, "bold"), padding=(18, 8))
        style.configure("green.Horizontal.TProgressbar", troughcolor="#dfe3ec", background=GREEN, thickness=14)
        style.configure("blue.Horizontal.TProgressbar", troughcolor="#dfe3ec", background=BLUE, thickness=14)

        header = tk.Frame(root, bg=NAVY)
        header.pack(fill="x")
        tk.Label(header, text=f"{OLT_NAME} Manager", bg=NAVY, fg="white",
                 font=("Segoe UI", 18, "bold")).pack(anchor="w", padx=22, pady=(14, 0))
        tk.Label(header, text=f"IP: {CURRENT_IP}",
                 bg=NAVY, fg="#b9c2d9", font=("Segoe UI", 10)).pack(anchor="w", padx=22, pady=(0, 14))

        self.paned = tk.PanedWindow(root, orient="vertical", bg=BG, sashwidth=6, sashrelief="flat")
        self.paned.pack(fill="both", expand=True, padx=14, pady=12)

        self.nb = ttk.Notebook(self.paned)
        self.paned.add(self.nb, stretch="always", minsize=300)

        log_wrap = tk.Frame(self.paned, bg=BG)
        self.paned.add(log_wrap, stretch="never", minsize=120, height=190)
        tk.Label(log_wrap, text="LIVE LOG", bg=BG, fg=GREY, font=("Segoe UI", 9, "bold")).pack(anchor="w", pady=(4, 2))
        self.log_box = scrolledtext.ScrolledText(log_wrap, state="disabled", font=("Consolas", 10),
                                                 bg="#101524", fg="#d6dbe8", relief="flat", padx=10, pady=8)
        self.log_box.pack(fill="both", expand=True)
        self.log_box.tag_config("ok", foreground="#5fd38d")
        self.log_box.tag_config("warn", foreground="#ff7b7b", font=("Consolas", 10, "bold"))
        self.log_box.tag_config("head", foreground="#6cb4ff", font=("Consolas", 10, "bold"))
        self.log_box.tag_config("dim", foreground="#8791ab")

        self._build_add_tab()
        self._build_4port_tab()  
        self._build_delete_tab() 
        self._build_fix_tab()
        self._build_optical_tab()
        self._build_vlan_tab() 

    def _card(self, parent, col, title, value, color):
        f = tk.Frame(parent, bg="white", highlightbackground="#dde2ee", highlightthickness=1)
        f.grid(row=0, column=col, sticky="nsew", padx=5)
        tk.Label(f, text=title, bg="white", fg=GREY, font=("Segoe UI", 9, "bold")).pack(anchor="w", padx=14, pady=(10, 0))
        lbl = tk.Label(f, text=value, bg="white", fg=color, font=("Segoe UI", 18, "bold"))
        lbl.pack(anchor="w", padx=14, pady=(0, 10))
        return lbl

    def _table(self, parent, cols, widths, stretch_col=None):
        wrap = tk.Frame(parent, bg="white")
        wrap.pack(fill="both", expand=True, padx=6, pady=6)
        tree = ttk.Treeview(wrap, columns=cols, show="headings")
        for c, w in zip(cols, widths):
            tree.heading(c, text=c)
            tree.column(c, width=w, minwidth=60, anchor="center", stretch=(c == stretch_col or stretch_col is None))
        sb = ttk.Scrollbar(wrap, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=sb.set)
        tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        return tree

    def _button(self, parent, key, text, color, hover, command):
        b = tk.Button(parent, text=text, font=("Segoe UI", 11, "bold"), bg=color, fg="white",
                      activebackground=hover, activeforeground="white", relief="flat",
                      cursor="hand2", padx=22, pady=8, command=command)
        self.buttons[key] = (b, color, text)
        return b

    # ---------- Tab 1: Auto Add ----------
    def _build_add_tab(self):
        tab = tk.Frame(self.nb, bg=BG)
        self.nb.add(tab, text="  Auto Add  ")

        cards = tk.Frame(tab, bg=BG)
        cards.pack(fill="x", padx=8, pady=(12, 6))
        for i in range(3):
            cards.columnconfigure(i, weight=1, uniform="c")
        self.card_status = self._card(cards, 0, "STATUS", "Ready", GREY)
        self.card_added = self._card(cards, 1, "ONU ADDED", "0", GREEN)
        self.card_warn = self._card(cards, 2, "CONFLICTS", "0", GREY)

        ctrl = tk.Frame(tab, bg=BG)
        ctrl.pack(fill="x", padx=8, pady=(6, 4))
        self._button(ctrl, "add", "START SCAN & ADD", GREEN, "#25804a", self.on_add).pack(side="left")
        self.pbar = ttk.Progressbar(ctrl, style="green.Horizontal.TProgressbar", mode="determinate")
        self.pbar.pack(side="left", fill="x", expand=True, padx=(16, 0))
        self.pct = tk.Label(ctrl, text="0%", bg=BG, fg=GREY, font=("Segoe UI", 10, "bold"), width=5)
        self.pct.pack(side="left")

        self.banner = tk.Label(tab, bg="#fdeaea", fg=RED, font=("Segoe UI", 10, "bold"),
                               anchor="w", justify="left", padx=14, pady=8, wraplength=860)

        self.tables = tk.PanedWindow(tab, orient="horizontal", bg=BG, sashwidth=6)
        self.tables.pack(fill="both", expand=True, padx=8, pady=(8, 8))

        left = tk.Frame(self.tables, bg="white")
        tk.Label(left, text="Added ONUs", bg="white", fg=NAVY, font=("Segoe UI", 10, "bold")).pack(anchor="w", padx=8, pady=(6, 0))
        self.tree_added = self._table(left, ("Slot", "PON", "ONU ID", "VLAN"), (80, 80, 80, 80))
        self.tables.add(left, stretch="always", minsize=260)

        right = tk.Frame(self.tables, bg="white")
        tk.Label(right, text="Conflicts (already configured elsewhere)", bg="white", fg=RED,
                 font=("Segoe UI", 10, "bold")).pack(anchor="w", padx=8, pady=(6, 0))
        self.tree_warn = self._table(right, ("Slot", "PON", "MAC / SN"), (70, 70, 190), stretch_col="MAC / SN")
        self.tree_warn.tag_configure("w", foreground=RED)
        bar = tk.Frame(right, bg="white")
        bar.pack(fill="x", padx=6, pady=(0, 6))
        tk.Button(bar, text="Copy all MAC/SN", relief="flat", bg="#e9edf5", cursor="hand2",
                  padx=12, pady=4, command=self.copy_macs).pack(side="left")
        tk.Button(bar, text="Go to Conflict Fix  >", relief="flat", bg="#e3ecfd", fg=BLUE, cursor="hand2",
                  padx=12, pady=4, command=lambda: self.nb.select(3)).pack(side="left", padx=8)
        self.tables.add(right, stretch="always", minsize=300)

    # ---------- Tab 2: 4-Port ONU ----------
    def _build_4port_tab(self):
        tab = tk.Frame(self.nb, bg=BG)
        self.nb.add(tab, text="  4-Port Config  ")

        info = tk.Label(tab, bg="#e8f0fe", fg=NAVY, font=("Segoe UI", 10), anchor="w", justify="left",
                        padx=14, pady=10, wraplength=860,
                        text="MAC/SN din. Script auto autofind theke (ba purano config delete kore) ONU khuje ber korbe. "
                             "Erpor Profile ID hisebe VLAN set korbe abong ETH 1, 2, 3, 4 charoti port ei Native VLAN config kore dibe.")
        info.pack(fill="x", padx=8, pady=(12, 6))

        box = tk.Frame(tab, bg="white", highlightbackground="#dde2ee", highlightthickness=1)
        box.pack(fill="x", padx=8, pady=14)
        
        tk.Label(box, text="4-Port ONU Serial Number (SN / MAC)", bg="white", fg=GREY,
                 font=("Segoe UI", 9, "bold")).pack(anchor="w", padx=16, pady=(12, 2))
        
        row = tk.Frame(box, bg="white")
        row.pack(fill="x", padx=16, pady=(0, 14))
        self.port4_entry = tk.Entry(row, font=("Consolas", 14), relief="solid", bd=1)
        self.port4_entry.pack(side="left", fill="x", expand=True, ipady=6)
        self.port4_entry.bind("<Return>", lambda e: self.on_4port())
        self._button(row, "port4", "CONFIGURE 4-PORT ONU", BLUE, "#2459b5", self.on_4port).pack(side="left", padx=(12, 0))

        tk.Label(tab, text="Configuration History (ei session)", bg=BG, fg=NAVY, font=("Segoe UI", 10, "bold")).pack(anchor="w", padx=10)
        wrap = tk.Frame(tab, bg="white")
        wrap.pack(fill="both", expand=True, padx=8, pady=(4, 8))
        self.tree_4port = self._table(wrap, ("Time", "SN", "Slot", "PON", "ONU ID", "VLAN (All 4 Ports)"), (90, 180, 80, 80, 80, 150), stretch_col="SN")

    # ---------- Tab 3: Delete & Bulk Delete ----------
    def _build_delete_tab(self):
        tab = tk.Frame(self.nb, bg=BG)
        self.nb.add(tab, text="  Delete / Bulk Delete  ")

        box1 = tk.Frame(tab, bg="white", highlightbackground="#dde2ee", highlightthickness=1)
        box1.pack(fill="x", padx=8, pady=(14, 8))
        tk.Label(box1, text="DELETE SINGLE ONU (By MAC/SN)", bg="white", fg=NAVY,
                 font=("Segoe UI", 10, "bold")).pack(anchor="w", padx=16, pady=(12, 2))
        row1 = tk.Frame(box1, bg="white")
        row1.pack(fill="x", padx=16, pady=(0, 14))
        self.sn_entry = tk.Entry(row1, font=("Consolas", 14), relief="solid", bd=1)
        self.sn_entry.pack(side="left", fill="x", expand=True, ipady=6)
        self.sn_entry.bind("<Return>", lambda e: self.on_delete())
        self._button(row1, "del", "DELETE SINGLE", RED, "#b53535", self.on_delete).pack(side="left", padx=(12, 0))

        ttk.Separator(tab, orient="horizontal").pack(fill="x", padx=20, pady=5)

        box2 = tk.Frame(tab, bg="#fff0f0", highlightbackground="#ffcccc", highlightthickness=1)
        box2.pack(fill="x", padx=8, pady=(8, 14))
        tk.Label(box2, text="BULK DELETE (PON-er Sob ONU Ekshathe Delete)", bg="#fff0f0", fg=RED,
                 font=("Segoe UI", 10, "bold")).pack(anchor="w", padx=16, pady=(12, 2))
        
        row2 = tk.Frame(box2, bg="#fff0f0")
        row2.pack(fill="x", padx=16, pady=(0, 14))
        
        tk.Label(row2, text="Slot:", bg="#fff0f0", font=("Segoe UI", 10, "bold")).pack(side="left")
        self.bulk_slot_cb = ttk.Combobox(row2, font=("Consolas", 12), width=10, state="readonly")
        self.bulk_slot_cb.pack(side="left", padx=(5, 20), ipady=3)
        
        tk.Label(row2, text="PON:", bg="#fff0f0", font=("Segoe UI", 10, "bold")).pack(side="left")
        self.bulk_pon_cb = ttk.Combobox(row2, font=("Consolas", 12), width=10, state="readonly")
        self.bulk_pon_cb['values'] = [str(i) for i in range(16)]
        self.bulk_pon_cb.pack(side="left", padx=(5, 20), ipady=3)
        
        self._button(row2, "bulk_del", "BULK DELETE PON", RED, "#b53535", self.on_bulk_delete).pack(side="left")

        tk.Label(tab, text="Deleted history (ei session)", bg=BG, fg=NAVY, font=("Segoe UI", 10, "bold")).pack(anchor="w", padx=10)
        wrap = tk.Frame(tab, bg="white")
        wrap.pack(fill="both", expand=True, padx=8, pady=(4, 8))
        self.tree_del = self._table(wrap, ("Time", "Action", "Target", "Status"), (90, 150, 250, 100), stretch_col="Target")

    def _build_fix_tab(self):
        tab = tk.Frame(self.nb, bg=BG)
        self.nb.add(tab, text="  Conflict Fix  ")

        info = tk.Label(tab, bg="#e8f0fe", fg=NAVY, font=("Segoe UI", 10), anchor="w", justify="left",
                        padx=14, pady=10, wraplength=860,
                        text="Autofind e thaka ONU jodi OLT er onno kothao age config kora thake, "
                             "ei tool setake khuje ber korbe, purano config (service-port + ONU) delete korbe, "
                             "tarpor notun kore config kore config save korbe.")
        info.pack(fill="x", padx=8, pady=(12, 6))

        ctrl = tk.Frame(tab, bg=BG)
        ctrl.pack(fill="x", padx=8, pady=6)
        self._button(ctrl, "fix", "FIND, DELETE & RE-CONFIG", BLUE, "#2459b5", self.on_fix).pack(side="left")
        self.fix_bar = ttk.Progressbar(ctrl, style="blue.Horizontal.TProgressbar", mode="indeterminate")
        self.fix_bar.pack(side="left", fill="x", expand=True, padx=(16, 0))

        wrap = tk.Frame(tab, bg="white")
        wrap.pack(fill="both", expand=True, padx=8, pady=(8, 8))
        self.tree_fix = self._table(wrap, ("MAC / SN", "Old location", "New location", "VLAN", "Status"),
                                    (170, 190, 190, 70, 150), stretch_col="Old location")
        self.tree_fix.tag_configure("fixed", foreground=GREEN)
        self.tree_fix.tag_configure("pending", foreground=AMBER)

    def _build_optical_tab(self):
        tab = tk.Frame(self.nb, bg=BG)
        self.nb.add(tab, text="  Optical Info  ")

        info = tk.Label(tab, bg="#e8eff5", fg=NAVY, font=("Segoe UI", 10), anchor="w", justify="left",
                        padx=14, pady=10, wraplength=860,
                        text="Shudhu MikroTik er PPPoE Username likhun. Script auto MikroTik theke user kon VLAN er under e ache ta ber korbe, "
                             "router er MAC address nibe, tarpor OLT er oi VLAN MAC table theke ONU er Optical Power ber korbe.")
        info.pack(fill="x", padx=8, pady=(12, 6))

        box = tk.Frame(tab, bg="white", highlightbackground="#dde2ee", highlightthickness=1)
        box.pack(fill="x", padx=8, pady=(6, 14))
        
        row1 = tk.Frame(box, bg="white")
        row1.pack(fill="x", padx=16, pady=(12, 2))
        tk.Label(row1, text="MikroTik PPPoE Username", bg="white", fg=GREY, width=32, anchor="w", font=("Segoe UI", 9, "bold")).pack(side="left")
        
        row2 = tk.Frame(box, bg="white")
        row2.pack(fill="x", padx=16, pady=(0, 14))
        self.opt_entry = tk.Entry(row2, font=("Consolas", 14), relief="solid", bd=1)
        self.opt_entry.pack(side="left", fill="x", expand=True, ipady=6, padx=(0, 12))
        
        self.opt_entry.bind("<Return>", lambda e: self.on_optical())
        self._button(row2, "opt", "GET OPTICAL INFO", BLUE, "#2459b5", self.on_optical).pack(side="left")

        cards = tk.Frame(tab, bg=BG)
        cards.pack(fill="x", padx=8, pady=6)
        for i in range(4):
            cards.columnconfigure(i, weight=1, uniform="c")
        
        self.card_rx = self._card(cards, 0, "Rx POWER (dBm)", "-", BLUE)
        self.card_tx = self._card(cards, 1, "Tx POWER (dBm)", "-", GREEN)
        self.card_tmp = self._card(cards, 2, "TEMPERATURE (°C)", "-", AMBER)
        self.card_onu = self._card(cards, 3, "ONU LOCATION", "-", NAVY)

    def _build_vlan_tab(self):
        tab = tk.Frame(self.nb, bg=BG)
        self.nb.add(tab, text="  OLT Config  ")

        info = tk.Label(tab, bg="#e8eff5", fg=NAVY, font=("Segoe UI", 10), anchor="w", justify="left",
                        padx=14, pady=10, wraplength=860,
                        text=f"Ei {OLT_NAME} ({CURRENT_IP}) er jonno nicher box theke Slot onujayi Config add ba update korun. "
                             f"'Auto Add' ba 'Conflict Fix' er somoy ei settings onujayi auto config generate hobe.")
        info.pack(fill="x", padx=8, pady=(12, 6))

        box = tk.Frame(tab, bg="white", highlightbackground="#dde2ee", highlightthickness=1)
        box.pack(fill="x", padx=8, pady=6)
        
        form_frame = tk.Frame(box, bg="white")
        form_frame.pack(fill="x", padx=16, pady=10)

        labels = ["Slot No", "VLAN Start", "VLAN End", "Inc/PON", "Line Profile", "Srv Profile"]
        for idx, text in enumerate(labels):
            tk.Label(form_frame, text=text, bg="white", fg=GREY, font=("Segoe UI", 9, "bold")).grid(row=0, column=idx, padx=5, sticky="w")

        self.v_slot_ent = tk.Entry(form_frame, font=("Consolas", 14), relief="solid", bd=1, width=8)
        self.v_slot_ent.grid(row=1, column=0, padx=5, pady=(2, 10), ipady=4)
        
        self.v_start_ent = tk.Entry(form_frame, font=("Consolas", 14), relief="solid", bd=1, width=10)
        self.v_start_ent.grid(row=1, column=1, padx=5, pady=(2, 10), ipady=4)

        self.v_end_ent = tk.Entry(form_frame, font=("Consolas", 14), relief="solid", bd=1, width=10)
        self.v_end_ent.grid(row=1, column=2, padx=5, pady=(2, 10), ipady=4)
        
        self.v_inc_ent = tk.Entry(form_frame, font=("Consolas", 14), relief="solid", bd=1, width=8)
        self.v_inc_ent.grid(row=1, column=3, padx=5, pady=(2, 10), ipady=4)

        self.v_line_ent = tk.Entry(form_frame, font=("Consolas", 14), relief="solid", bd=1, width=10)
        self.v_line_ent.grid(row=1, column=4, padx=5, pady=(2, 10), ipady=4)

        self.v_srv_ent = tk.Entry(form_frame, font=("Consolas", 14), relief="solid", bd=1, width=10)
        self.v_srv_ent.grid(row=1, column=5, padx=5, pady=(2, 10), ipady=4)
        
        tk.Button(form_frame, text="SAVE", font=("Segoe UI", 11, "bold"), bg=GREEN, fg="white", 
                  relief="flat", cursor="hand2", padx=15, command=self.add_vlan).grid(row=1, column=6, padx=(15, 5), pady=(2, 10))
        
        wrap = tk.Frame(tab, bg="white")
        wrap.pack(fill="both", expand=True, padx=8, pady=(8, 8))
        
        self.tree_vlan = self._table(wrap, ("Slot", "VLAN Start", "VLAN End", "Increment", "Line Profile", "Srv Profile"), 
                                     (60, 100, 100, 100, 100, 100), stretch_col="VLAN Start")
        
        btn_bar = tk.Frame(tab, bg=BG)
        btn_bar.pack(fill="x", padx=8, pady=(0, 10))
        tk.Button(btn_bar, text="Delete Selected", relief="flat", bg=RED, fg="white", cursor="hand2",
                  padx=12, pady=4, font=("Segoe UI", 9, "bold"), command=self.delete_vlan).pack(side="left")
                  
        self.load_vlan_table()

    def update_bulk_slots(self):
        slots = sorted(list(olt_db.get(CURRENT_IP, {}).keys()))
        if not slots:
            slots = ["0/0", "0/1", "0/2", "0/3", "0/4"]
        if hasattr(self, 'bulk_slot_cb'):
            self.bulk_slot_cb['values'] = slots
            if slots:
                self.bulk_slot_cb.current(0)

    def load_vlan_table(self):
        self.tree_vlan.delete(*self.tree_vlan.get_children())
        configs = olt_db.get(CURRENT_IP, {})
        for slot in sorted(configs.keys()):
            cfg = configs[slot]
            self.tree_vlan.insert("", "end", iid=slot, values=(
                slot, 
                cfg["start"], 
                cfg.get("end", "-"),
                cfg["inc"], 
                cfg.get("line_profile", 100), 
                cfg.get("srv_profile", 100)
            ))
        self.update_bulk_slots()

    def add_vlan(self):
        slot = self.v_slot_ent.get().strip()
        v_start = self.v_start_ent.get().strip()
        v_end = self.v_end_ent.get().strip() 
        v_inc = self.v_inc_ent.get().strip()
        v_line = self.v_line_ent.get().strip()
        v_srv = self.v_srv_ent.get().strip()
        
        if not re.match(r"^\d+/\d+$", slot):
            messagebox.showerror("Error", "Slot format thik nai. Example: 0/1 ba 0/0")
            return
        if not v_start.isdigit() or not v_inc.isdigit() or not v_line.isdigit() or not v_srv.isdigit() or (v_end and not v_end.isdigit()):
            messagebox.showerror("Error", "Sombhoboto kono field e number chara onno kichu diyechen!")
            return
            
        if CURRENT_IP not in olt_db:
            olt_db[CURRENT_IP] = {}
            
        olt_db[CURRENT_IP][slot] = {
            "start": int(v_start),
            "end": int(v_end) if v_end else 0,
            "inc": int(v_inc),
            "line_profile": int(v_line),
            "srv_profile": int(v_srv)
        }
        save_db()
        self.load_vlan_table()
        
        self.v_slot_ent.delete(0, "end")
        self.v_start_ent.delete(0, "end")
        self.v_end_ent.delete(0, "end")
        self.v_inc_ent.delete(0, "end")
        self.v_line_ent.delete(0, "end")
        self.v_srv_ent.delete(0, "end")
        messagebox.showinfo("Success", f"Slot {slot} er Config update hoyeche!")

    def delete_vlan(self):
        selected = self.tree_vlan.selection()
        if not selected:
            messagebox.showerror("Error", "Kono slot select kora nai!")
            return
        slot = selected[0]
        if messagebox.askyesno("Confirm", f"Slot {slot} er config delete korben?"):
            if slot in olt_db.get(CURRENT_IP, {}):
                del olt_db[CURRENT_IP][slot]
                save_db()
                self.load_vlan_table()

    def log(self, msg, tag=None):
        def _w():
            self.log_box.config(state="normal")
            self.log_box.insert("end", msg + "\n", tag)
            self.log_box.see("end")
            self.log_box.config(state="disabled")
        self.root.after(0, _w)

    def status(self, text):
        self.root.after(0, lambda: self.card_status.config(text=text, fg=NAVY))

    def progress(self, step):
        def _p():
            self.pbar["value"] = step
            pct = int(step * 100 / self.total_steps) if self.total_steps > 0 else 0
            self.pct.config(text=f"{pct}%")
        self.root.after(0, _p)

    def add_row(self, slot, pon, onu, vlan):
        def _a():
            self.tree_added.insert("", "end", values=(slot, pon, onu, vlan))
            self.added_count += 1
            self.card_added.config(text=str(self.added_count))
        self.root.after(0, _a)

    def warn_row(self, slot, pon, mac):
        def _w():
            self.tree_warn.insert("", "end", values=(slot, pon, mac), tags=("w",))
            self.warn_count += 1
            self.card_warn.config(text=str(self.warn_count), fg=RED)
            self.banner.config(text=f"  \u26A0  {self.warn_count} ta ONU age theke OLT e config kora ache. "
                                    f"'Conflict Fix' tab diye auto delete + re-config korte paren.")
            if not self.banner.winfo_ismapped():
                self.banner.pack(fill="x", padx=8, pady=(8, 0), before=self.tables)
        self.root.after(0, _w)

    def conflict_row(self, sn, old, status):
        def _c():
            if self.tree_fix.exists(sn):
                self.tree_fix.delete(sn)
            self.tree_fix.insert("", "end", iid=sn, values=(sn, old, "-", "-", status), tags=("pending",))
        self.root.after(0, _c)

    def conflict_update(self, sn, new_loc, vlan, status):
        def _u():
            if self.tree_fix.exists(sn):
                old = self.tree_fix.item(sn)["values"][1]
                tag = "fixed" if status == "Fixed" else "pending"
                self.tree_fix.item(sn, values=(sn, old, new_loc, vlan, status), tags=(tag,))
        self.root.after(0, _u)

    def set_busy(self, active_key=None):
        self.running = active_key is not None
        for key, (b, color, text) in self.buttons.items():
            if self.running:
                b.config(state="disabled", bg=GREY_BTN, text="RUNNING..." if key == active_key else text)
            else:
                b.config(state="normal", bg=color, text=text)
        
        state = "disabled" if self.running else "normal"
        self.sn_entry.config(state=state)
        self.port4_entry.config(state=state)
        self.opt_entry.config(state=state)
        
        self.bulk_slot_cb.config(state="disabled" if self.running else "readonly")
        self.bulk_pon_cb.config(state="disabled" if self.running else "readonly")
        
        self.v_slot_ent.config(state=state)
        self.v_start_ent.config(state=state)
        self.v_end_ent.config(state=state)
        self.v_inc_ent.config(state=state)
        self.v_line_ent.config(state=state)
        self.v_srv_ent.config(state=state)

    def copy_macs(self):
        macs = [self.tree_warn.item(i)["values"][2] for i in self.tree_warn.get_children()]
        if not macs:
            messagebox.showinfo("Copy", "Kono conflict nai.")
            return
        self.root.clipboard_clear()
        self.root.clipboard_append("\n".join(str(m) for m in macs))
        messagebox.showinfo("Copied", f"{len(macs)} ta MAC/SN copy hoyeche.")

    def on_add(self):
        if self.running:
            return
            
        slots = list(olt_db.get(CURRENT_IP, {}).keys())
        if not slots:
            messagebox.showerror("Error", "Kono Slot configure kora nai! Age 'OLT Config' tab e giye configure korun.")
            return
            
        if not messagebox.askyesno("Confirm", f"Config kora shob slot ({', '.join(slots)}) scan kore notun ONU add korte chan?"):
            return
            
        for t in (self.tree_added, self.tree_warn):
            t.delete(*t.get_children())
            
        self.added_count = self.warn_count = 0
        self.card_added.config(text="0")
        self.card_warn.config(text="0", fg=GREY)
        
        self.total_steps = len(slots) * 16
        self.pbar["maximum"] = self.total_steps
        self.pbar["value"] = 0
        self.pct.config(text="0%")
        
        self.banner.pack_forget()
        self.card_status.config(text="Connecting...", fg=NAVY)
        self.set_busy("add")

        def worker():
            err = job_scan_and_add(self)
            self.root.after(0, lambda: self.finish_add(err))
        threading.Thread(target=worker, daemon=True).start()

    def finish_add(self, error):
        self.set_busy(None)
        if error:
            self.card_status.config(text="Error", fg=RED)
            messagebox.showerror("Error", f"An error occurred:\n{error}")
            return
        self.progress(self.total_steps)
        if self.warn_count:
            self.card_status.config(text="Done (Conflict)", fg=AMBER)
            messagebox.showwarning(
                "Done - Conflict paoa gese",
                f"{self.added_count} ta ONU add hoyeche.\n\n"
                f"{self.warn_count} ta ONU age theke OLT e config kora ache.\n"
                f"'Conflict Fix' tab diye ota fix korun.")
        else:
            self.card_status.config(text="Completed", fg=GREEN)
            messagebox.showinfo("Done", f"{self.added_count} ta ONU add hoyeche.\nConfiguration saved.")

    def on_4port(self):
        if self.running:
            return
        sn = self.port4_entry.get().strip().upper()
        if not re.fullmatch(r"[0-9A-F]{16}", sn):
            messagebox.showerror("Error", "SN 16 ta character er hote hobe (0-9, A-F).")
            return
        if not messagebox.askyesno("Confirm", f"4-Port ONU ({sn}) config korben?"):
            return
            
        self.set_busy("port4")
        
        def worker():
            res = job_4port_config(self, sn)
            self.root.after(0, lambda: self.finish_4port(sn, res))
        threading.Thread(target=worker, daemon=True).start()
        
    def finish_4port(self, sn, res):
        self.set_busy(None)
        state, data = res
        if state == "ok":
            slot, pon, onu, vlan = data
            self.tree_4port.insert("", 0, values=(time.strftime("%H:%M:%S"), sn, slot, pon, onu, vlan))
            self.port4_entry.delete(0, "end")
            messagebox.showinfo("Success", f"4-Port ONU setup completed!\n\nSN: {sn}\nLocation: {slot}/{pon}/{onu}\nNative VLAN: {vlan}")
        else:
            messagebox.showerror("Error", f"Failed to config 4-Port ONU:\n{data}")

    def on_delete(self):
        if self.running:
            return
        sn = self.sn_entry.get().strip().upper()
        if not re.fullmatch(r"[0-9A-F]{16}", sn):
            messagebox.showerror("Error", "SN 16 ta character er hote hobe (0-9, A-F).")
            return
        if not messagebox.askyesno("Confirm", f"ONU {sn} permanently delete korben?"):
            return
        self.set_busy("del")

        def worker():
            res = job_delete(self, sn)
            self.root.after(0, lambda: self.finish_delete(sn, res))
        threading.Thread(target=worker, daemon=True).start()

    def finish_delete(self, sn, res):
        self.set_busy(None)
        state, data = res
        if state == "ok":
            slot, pon, onu = data
            self.tree_del.insert("", 0, values=(time.strftime("%H:%M:%S"), "Single Delete", f"SN: {sn} ({slot}/{pon}/{onu})", "Success"))
            self.sn_entry.delete(0, "end")
            messagebox.showinfo("Deleted", f"ONU deleted successfully!\n\nSN: {sn}\nSlot: {slot}\n"
                                           f"PON: {pon}\nONU ID: {onu}\n\nConfiguration saved.")
        elif state == "notfound":
            messagebox.showerror("Not found", f"ONU {sn} OLT te paoa jayni.")
        else:
            messagebox.showerror("Error", f"An error occurred:\n{data}")

    def on_bulk_delete(self):
        if self.running:
            return
        slot = self.bulk_slot_cb.get()
        pon = self.bulk_pon_cb.get()
        
        if not slot or not pon:
            messagebox.showerror("Error", "Slot ar PON select korun.")
            return
            
        if not messagebox.askyesno("WARNING", f"SABDHAN!\n\nApni ki sotti e Slot {slot} PON {pon} er SOB ONU eksathe delete korte chan?"):
            return
            
        self.set_busy("bulk_del")
        
        def worker():
            res = job_bulk_delete(self, slot, pon)
            self.root.after(0, lambda: self.finish_bulk_delete(slot, pon, res))
        threading.Thread(target=worker, daemon=True).start()
        
    def finish_bulk_delete(self, slot, pon, res):
        self.set_busy(None)
        if res == "ok":
            self.tree_del.insert("", 0, values=(time.strftime("%H:%M:%S"), "Bulk Delete", f"Slot {slot} PON {pon}", "Success"))
            messagebox.showinfo("Success", f"Slot {slot} PON {pon} er sob ONU delete kora hoyeche.")
        else:
            messagebox.showerror("Error", "Bulk delete e shomossha hoyeche. Log check korun.")

    def on_fix(self):
        if self.running:
            return
        if not messagebox.askyesno(
                "Confirm",
                "Autofind e thaka ONU gulo jodi onno kothao config kora thake,\n"
                "purano config DELETE kore notun kore config kora hobe.\n\nChalate chan?"):
            return
        self.tree_fix.delete(*self.tree_fix.get_children())
        self.set_busy("fix")
        self.fix_bar.start(12)

        def worker():
            res = job_conflict_fix(self)
            self.root.after(0, lambda: self.finish_fix(res))
        threading.Thread(target=worker, daemon=True).start()

    def finish_fix(self, res):
        self.fix_bar.stop()
        self.set_busy(None)
        self.card_status.config(text="Ready", fg=GREY)
        if isinstance(res, str):
            messagebox.showerror("Error", f"An error occurred:\n{res}")
            return
        fixed, deleted = res
        if deleted == 0:
            messagebox.showinfo("Conflict Fix", "Kono conflict paoa jayni.")
        else:
            messagebox.showinfo("Conflict Fix",
                                f"{deleted} ta conflict ONU er purano config delete hoyeche.\n"
                                f"{fixed} ta ONU notun kore config hoyeche.\nConfiguration saved.")

    def on_optical(self):
        if self.running:
            return
            
        username = self.opt_entry.get().strip()
        
        if not username:
            messagebox.showerror("Error", "Doya kore PPPoE username likhun.")
            return
            
        self.card_rx.config(text="-", fg=BLUE)
        self.card_tx.config(text="-", fg=GREEN)
        self.card_tmp.config(text="-", fg=AMBER)
        self.card_onu.config(text="-", fg=NAVY)
        
        self.set_busy("opt")
        
        def worker():
            res = job_optical_info(self, username)
            self.root.after(0, lambda: self.finish_optical(res))
        threading.Thread(target=worker, daemon=True).start()

    def finish_optical(self, res):
        self.set_busy(None)
        state, data = res
        if state == "ok":
            rx, tx, tmp, loc = data
            
            try:
                rx_float = float(rx)
                rx_color = RED if rx_float < -27 else GREEN
            except ValueError:
                rx_color = BLUE
                
            self.card_rx.config(text=f"{rx}", fg=rx_color)
            self.card_tx.config(text=f"{tx}")
            self.card_tmp.config(text=f"{tmp}")
            
            messagebox.showinfo("Success", f"Optical information successfully fetched!\n\nLocation: {loc}")
        else:
            messagebox.showerror("Error", f"Information pawa jayni:\n{data}")


if __name__ == "__main__":
    root = tk.Tk()
    root.withdraw() 
    
    def on_login_success():
        root.deiconify() 
        App(root)
        
    LoginDialog(root, on_login_success)
    root.mainloop()