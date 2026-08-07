#!/usr/bin/env python3
"""
感知评分实验 — 400格网池 v4
每组实验: 1锚定 + 4练习 + 15正式 = 20组
每格网目标: 5次独立评分
"""
import os, sys, json, csv, time, random, socket, re
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse

HERE = Path(__file__).parent
DATA_DIR = HERE / "data"
IMG_DIR = HERE / "images"

# --- Read CSVs ---
def read_csv(path):
    with open(path, newline='', encoding='utf-8') as f:
        return list(csv.DictReader(f))

SAMPLE = read_csv(DATA_DIR / "perception_rating_sample_v1.csv")
IMG_LIST = read_csv(DATA_DIR / "perception_rating_image_list_v1.csv")

# --- Image lookup: {grid_id: {direction_str: filename}} ---
IMG_LOOKUP = {}
GRID_LATLON = {}  # {grid_id: (lat, lon)}
for row in IMG_LIST:
    gid = row['grid_id']
    d = str(int(float(row['direction'])))
    fname = row['filename']
    IMG_LOOKUP.setdefault(gid, {})[d] = fname
    # Extract lat/lon from filename: {point_id}_{lon}_{lat}_{direction}_{date}.jpg
    if gid not in GRID_LATLON:
        m = re.match(r'\d+_([\d.]+)_([\d.]+)_\d+_\d+\.jpg', fname)
        if m:
            GRID_LATLON[gid] = (float(m.group(2)), float(m.group(1)))  # lat, lon

# --- Grid metadata ---
GRID_META = {}
for row in SAMPLE:
    gid = row['grid_id']
    GRID_META[gid] = {
        'district': row.get('district', ''),
        'ori_level': row.get('ori_level', ''),
        'urban_type': row.get('urban_type', ''),
    }

# --- AI reference scores (computed from sample features) ---
ALL_GRIDS_POOL = sorted(IMG_LOOKUP.keys())
random.seed(20260807)

def compute_refs():
    """Compute 1-5 reference scores from available features."""
    refs = {}
    # Collect feature values
    fld_vals = []
    geo_vals = []
    fir_vals = []
    for row in SAMPLE:
        gid = row['grid_id']
        if gid not in IMG_LOOKUP:
            continue
        try:
            fld_vals.append((gid, float(row.get('ORI_FLD_pct', 50))))
        except: pass
        try:
            geo_vals.append((gid, float(row.get('SWI', 0))))
        except: pass
        try:
            fir_vals.append((gid, float(row.get('BEI', 0))))
        except: pass

    # Sort and assign percentile-based scores 1-5
    for label, pairs in [('FLD', fld_vals), ('GEO', geo_vals), ('FIR', fir_vals)]:
        sorted_pairs = sorted(pairs, key=lambda x: x[1])
        n = len(sorted_pairs)
        for rank, (gid, _) in enumerate(sorted_pairs):
            score = max(1, min(5, round(rank / max(n-1, 1) * 4 + 1)))
            refs.setdefault(gid, {})[label] = score

    # Fill missing with 3
    for gid in ALL_GRIDS_POOL:
        if gid not in refs:
            refs[gid] = {'FLD': 3, 'GEO': 3, 'FIR': 3}
    return refs

REF_SCORES = compute_refs()

# --- Fixed anchor and practice grids ---
# Anchor: 1 extreme grid (high FLD + high FIR)
ANCHOR_GRID = 'R152C029' if 'R152C029' in ALL_GRIDS_POOL else ALL_GRIDS_POOL[0]
# Practice: 4 grids covering different risk profiles, with known refs
PRACTICE_GRIDS = []
practice_candidates = [g for g in ['R099C070', 'R117C010', 'R161C064', 'R102C103',
                                     'R137C067', 'R092C045', 'R139C067', 'R122C057']
                       if g in ALL_GRIDS_POOL]
if len(practice_candidates) >= 4:
    PRACTICE_GRIDS = practice_candidates[:4]
else:
    PRACTICE_GRIDS = [g for g in ALL_GRIDS_POOL if g != ANCHOR_GRID][:4]

# --- Rating count tracking ---
COUNTS_PATH = DATA_DIR / "rating_counts.json"

def load_counts():
    if COUNTS_PATH.exists():
        return json.loads(COUNTS_PATH.read_text())
    return {}

def save_counts(counts):
    COUNTS_PATH.write_text(json.dumps(counts, ensure_ascii=False, indent=2))

def get_least_rated(n=15):
    """Get n grids with fewest ratings from the pool."""
    counts = load_counts()
    # All grids in pool, sorted by count (missing = 0)
    pool = [(counts.get(g, 0), g) for g in ALL_GRIDS_POOL
            if g not in [ANCHOR_GRID] + PRACTICE_GRIDS]
    random.shuffle(pool)  # randomize within same count
    pool.sort(key=lambda x: x[0])
    selected = [g for _, g in pool[:n]]
    return selected

# --- Active session tracking (prevent same grids assigned twice) ---
ACTIVE_ASSIGNMENTS = {}  # {pid: [grid_ids]} — cleared on save

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args): pass

    def do_GET(self):
        p = urlparse(self.path).path
        if p in ('/', '/index.html'): self._html()
        elif p == '/admin': self._admin()
        elif p.startswith('/img/'): self._image(p)
        elif p.startswith('/ref/'): self._ref(p)
        elif p.startswith('/download/'): self._download(p)
        elif p == '/api/data': self._api_data()
        elif p.startswith('/api/meta/'): self._api_meta(p)
        else: self.send_error(404)

    def do_POST(self):
        if self.path == '/save': self._save()
        elif self.path == '/save-one': self._save_one()
        else: self.send_error(404)

    def _json(self, data):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers(); self.wfile.write(body)

    def _html(self):
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.end_headers(); self.wfile.write(HTML.encode())

    def _image(self, p):
        parts = p.split('/')
        if len(parts) >= 4:
            gid, direction = parts[2], parts[3]
            lookup = IMG_LOOKUP.get(gid, {})
            fname = lookup.get(direction) or lookup.get(str(int(direction) if direction.isdigit() else 0))
            if not fname: fname = next(iter(lookup.values()), None)
            if fname:
                img_path = IMG_DIR / fname
                if img_path.exists():
                    data = img_path.read_bytes()
                    self.send_response(200)
                    self.send_header('Content-Type', 'image/jpeg')
                    self.send_header('Content-Length', str(len(data)))
                    self.send_header('Cache-Control', 'max-age=86400')
                    self.end_headers(); self.wfile.write(data)
                    return
        self.send_error(404)

    def _ref(self, p):
        gid = p.split('/')[-1]
        ref = REF_SCORES.get(gid, {'FLD': 4, 'GEO': 4, 'FIR': 4})
        meta = GRID_META.get(gid, {})
        latlon = GRID_LATLON.get(gid, (None, None))
        self._json({'ref': ref, 'meta': meta, 'lat': latlon[0], 'lon': latlon[1]})

    def _api_data(self):
        """Assign grids for a new rater session."""
        main_grids = get_least_rated(15)
        self._json({
            'anchor': [ANCHOR_GRID],
            'training': PRACTICE_GRIDS,
            'main': main_grids,
            'refs': REF_SCORES,
            'grid_meta': GRID_META,
            'grid_latlon': GRID_LATLON,
            'attention_check': PRACTICE_GRIDS[1] if len(PRACTICE_GRIDS) > 1 else None,
            'attention_position': 10,
            'attention_check_2': main_grids[3] if len(main_grids) > 3 else None,
            'attention_position_2': 13,
            'instr_check_grid': main_grids[6] if len(main_grids) > 6 else None,
            'instr_check_hazard': 'GEO',
            'total_pool': len(ALL_GRIDS_POOL),
            'remaining_under_5': sum(1 for g in ALL_GRIDS_POOL if load_counts().get(g, 0) < 5),
        })

    def _api_meta(self, p):
        gid = p.split('/')[-1]
        if gid in GRID_META:
            imgs = IMG_LOOKUP.get(gid, {})
            self._json({
                'grid_id': gid,
                'lat': GRID_LATLON.get(gid, (None, None))[0],
                'lon': GRID_LATLON.get(gid, (None, None))[1],
                'images': imgs,
                **GRID_META[gid]
            })
        else:
            self.send_error(404)

    def _admin(self):
        all_csvs = sorted(DATA_DIR.glob('*.csv'), reverse=True)
        rating_files = [f for f in all_csvs
                        if not f.name.startswith('perception_rating')]
        rows = ''
        for f in rating_files:
            name = f.name
            try:
                n = len(f.read_text().strip().split('\n')) - 1
            except: n = '?'
            rows += f'<tr><td>{name}</td><td>{n} ratings</td><td><a href="/download/{name}">Download</a></td></tr>'
        if not rows:
            rows = '<tr><td colspan="3">暂无数据。</td></tr>'

        # Show rating progress
        counts = load_counts()
        done_5 = sum(1 for c in counts.values() if c >= 5)
        done_any = sum(1 for c in counts.values() if c > 0)
        progress_html = f'<p>📊 格网覆盖: {done_any}/400 已有评分 | {done_5}/400 已满5次</p>'

        page = ADMIN_HTML.replace('{{ROWS}}', rows).replace('{{PROGRESS}}', progress_html)
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.end_headers()
        self.wfile.write(page.encode())

    def _download(self, p):
        fname = p.split('/')[-1]
        if not fname or '..' in fname:
            self.send_error(400); return
        fpath = DATA_DIR / fname
        if not fpath.exists():
            self.send_error(404); return
        data = fpath.read_bytes()
        ct = 'text/csv; charset=utf-8' if fname.endswith('.csv') else 'application/json; charset=utf-8'
        self.send_response(200)
        self.send_header('Content-Type', ct)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Content-Disposition', f'attachment; filename="{fname}"')
        self.end_headers()
        self.wfile.write(data)

    def _save_one(self):
        length = int(self.headers.get('Content-Length', 0))
        body = json.loads(self.rfile.read(length))
        pid = body.get('participant_id', 'unknown')
        gid = body.get('grid_id', '')
        csv_path = DATA_DIR / f'pretest_{pid}.csv'
        is_new = not csv_path.exists()

        # Get lat/lon for this grid
        lat, lon = GRID_LATLON.get(gid, (None, None))
        imgs = IMG_LOOKUP.get(gid, {})
        img_str = '|'.join(f'{d}:{fn}' for d, fn in sorted(imgs.items()))

        with open(csv_path, 'a', newline='', encoding='utf-8') as f:
            w = csv.writer(f)
            if is_new:
                w.writerow(['participant_id', 'phase', 'grid_id', 'lat', 'lon',
                            'images', 'district',
                            'is_attention_check', 'is_ac2', 'is_instr',
                            'instr_hazard', 'instr_passed',
                            'FLD', 'GEO', 'FIR', 'response_time_sec'])
            w.writerow([pid, body.get('phase', ''), gid,
                        lat, lon, img_str,
                        GRID_META.get(gid, {}).get('district', ''),
                        body.get('is_ac', ''), body.get('is_ac2', ''),
                        body.get('is_instr', ''), body.get('instr_hazard', ''),
                        body.get('instr_passed', ''),
                        body.get('FLD', ''), body.get('GEO', ''), body.get('FIR', ''),
                        round(float(body.get('response_time_sec', 0)), 1)])

        # Update rating count
        counts = load_counts()
        counts[gid] = counts.get(gid, 0) + 1
        save_counts(counts)

        # Save demographics on first call
        demo = body.get('demographics')
        if demo:
            demo_path = DATA_DIR / f'pretest_demo_{pid}.json'
            if not demo_path.exists():
                demo_path.write_text(json.dumps({
                    'participant_id': pid, 'demographics': demo
                }, ensure_ascii=False, indent=2))

        self._json({'status': 'ok', 'saved': gid,
                     'grid_ratings': counts[gid]})

    def _save(self):
        """保存反馈问卷"""
        length = int(self.headers.get('Content-Length', 0))
        body = json.loads(self.rfile.read(length))
        pid = body.get('participant_id', 'unknown')
        ts = time.strftime('%Y%m%d_%H%M%S')

        # Also save a backup CSV with all ratings (compat)
        ratings = body.get('ratings', [])
        if ratings:
            csv_path = DATA_DIR / f'pretest_{pid}_{ts}_backup.csv'
            with open(csv_path, 'w', newline='', encoding='utf-8') as f:
                w = csv.writer(f)
                w.writerow(['participant_id','phase','grid_id','is_attention_check',
                           'FLD','GEO','FIR','response_time_sec'])
                for r in ratings:
                    w.writerow([pid, r.get('phase',''), r.get('grid_id',''),
                               r.get('is_ac',''), r.get('FLD',''), r.get('GEO',''),
                               r.get('FIR',''), round(r.get('response_time_sec',0),1)])

        fb_path = DATA_DIR / f'pretest_feedback_{pid}_{ts}.json'
        fb_path.write_text(json.dumps({
            'participant_id': pid,
            'demographics': body.get('demographics', {}),
            'feedback': body.get('feedback', {}),
            'n_ratings': body.get('n_ratings', len(ratings)),
            'ac1_passed': body.get('ac1_passed'),
            'ac2_passed': body.get('ac2_passed'),
            'instr_passed': body.get('instr_passed'),
        }, ensure_ascii=False, indent=2))

        self._json({'status': 'ok', 'ratings_count': body.get('n_ratings', 0)})

# ============================================================
# HTML — 与 v3 相同，适配 1+4+15=20 组
# ============================================================
HTML = r'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>灾害风险感知评分</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif;
     background:#f0f2f5;color:#2c3e50;line-height:1.65;max-width:960px;margin:0 auto;padding:14px}
.top{background:linear-gradient(135deg,#1a252f,#2c3e50);color:#fff;padding:16px 22px;
     border-radius:10px;margin-bottom:14px;text-align:center}
.top h1{font-size:1.25em}.top .sub{font-size:.8em;opacity:.7;margin-top:2px}
.bar-wrap{background:#dfe6e9;border-radius:6px;height:5px;margin:10px 0}
.bar-fill{background:#00b894;height:100%;border-radius:6px;transition:width .3s}
.steps{display:flex;justify-content:center;gap:4px;margin:6px 0}
.steps span{padding:2px 10px;border-radius:10px;font-size:.72em;background:#dfe6e9;color:#636e72}
.steps span.on{background:#0984e3;color:#fff}
.card{background:#fff;border-radius:10px;padding:20px 24px;margin:12px 0;box-shadow:0 1px 3px rgba(0,0,0,.07)}
.imgs{display:grid;grid-template-columns:1fr 1fr;gap:5px;margin:12px 0}
.imgs div{position:relative;background:#000;border-radius:6px;overflow:hidden;aspect-ratio:4/3}
.imgs img{width:100%;height:100%;object-fit:cover;display:block}
.imgs .tag{position:absolute;top:5px;left:5px;background:rgba(0,0,0,.72);color:#fff;
           padding:1px 7px;border-radius:4px;font-size:.75em}
.q-block{margin:14px 0;padding:12px 16px;background:#f8fafc;border-radius:8px}
.q-block h3{font-size:.9em;margin-bottom:2px}
.q-block .tip{font-size:.74em;color:#888;margin-bottom:6px}
.scale{display:flex;justify-content:space-between;gap:0}
.scale label{flex:1;text-align:center;cursor:pointer;padding:3px 0;border-radius:5px;transition:background .12s}
.scale label:hover{background:#e8f0fe}
.scale input{display:none}
.scale .dot{display:block;width:32px;height:32px;line-height:32px;border-radius:50%;
            border:2px solid #b2bec3;margin:0 auto 2px;font-weight:700;font-size:.85em;transition:all .18s}
.scale input:checked+.dot{background:#0984e3;color:#fff;border-color:#0984e3;transform:scale(1.15)}
.scale .lbl{font-size:.6em;color:#888;line-height:1.25;white-space:pre-line}
.fb{display:none;padding:10px 14px;border-radius:6px;margin:10px 0;font-size:.85em}
.fb.show{display:block}
.fb.good{background:#e8f8f0;border:1px solid #00b894}
.fb.warn{background:#fef8e7;border:1px solid #fdcb6e}
.fb.bad{background:#fde8e8;border:1px solid #e17055}
.ref-box{background:#e8f4fd;border-left:4px solid #0984e3;padding:10px 14px;border-radius:4px;margin:10px 0;font-size:.85em}
.btn{display:inline-block;padding:10px 26px;border:none;border-radius:6px;font-size:.9em;cursor:pointer;transition:all .18s}
.btn-p{background:#0984e3;color:#fff}.btn-p:hover{background:#0773c5}
.btn-p:disabled{background:#b2bec3;cursor:not-allowed}
.btn-g{background:#00b894;color:#fff}.btn-g:hover{background:#00a381}
.btns{text-align:center;margin:16px 0}
.cue-toggle{text-align:center;margin:8px 0}
.cue-toggle button{background:none;border:1px dashed #b2bec3;color:#636e72;padding:5px 16px;border-radius:14px;
                   font-size:.78em;cursor:pointer}
.cue-card{display:none;background:#fafbfc;border:1px solid #dfe6e9;border-radius:8px;padding:14px 18px;margin:8px 0}
.cue-card.show{display:block}
.cue-card table{width:100%;font-size:.78em;border-collapse:collapse}
.cue-card th{background:#f0f2f5;padding:6px 8px;text-align:left;font-size:.85em}
.cue-card td{padding:5px 8px;border-bottom:1px solid #f0f2f5;vertical-align:top}
.briefing h2{font-size:1.1em;color:#2c3e50;margin:16px 0 8px}
.briefing h3{font-size:.95em;color:#0984e3;margin:12px 0 4px}
.briefing p{margin:6px 0;font-size:.9em}
.briefing .disaster-card{display:inline-block;width:31%;vertical-align:top;padding:10px 12px;
    margin:6px 1%;background:#f8fafc;border-radius:8px;border-top:3px solid #0984e3}
.briefing .disaster-card h4{font-size:.85em;margin-bottom:4px}
.briefing .disaster-card p{font-size:.78em;color:#555;margin:2px 0}
.briefing .warn-box{background:#fff3cd;border:1px solid #fdcb6e;padding:10px 14px;border-radius:6px;
    font-size:.85em;margin:10px 0}
.fb-form label{display:block;font-weight:600;margin:12px 0 3px;font-size:.85em}
.fb-form textarea{width:100%;padding:9px;border:1px solid #dfe6e9;border-radius:6px;font-size:.85em;resize:vertical}
.fb-form input,.fb-form select{width:100%;padding:8px;border:1px solid #dfe6e9;border-radius:6px;font-size:.85em}
.done{text-align:center;padding:24px}.done h2{color:#00b894;margin-bottom:8px}
.attn-warn{background:#ffeaa7;border:1px solid #fdcb6e;padding:10px 14px;border-radius:6px;
           font-size:.84em;margin:10px 0;display:none}
.attn-warn.show{display:block}
.pool-info{background:#e8f4fd;border-radius:6px;padding:8px 14px;font-size:.78em;color:#0984e3;text-align:center;margin:4px 0}
</style>
</head>
<body>
<div id="app"></div>
<script>
let D={};
let S={phase:'briefing',pid:'P01',age:'',cq:'',page:0,
       anchors:[],train:[],main:[],refs:{},attnGrid:null,attnPos:10,
       attnGrid2:null,attnPos2:13,instrGrid:null,instrHazard:'GEO',
       currentIdx:0,currentList:[],ratings:[],qStart:0,trainResults:[],
       totalPool:400,remaining:400};

fetch('/api/data').then(r=>r.json()).then(d=>{
    Object.assign(S,{anchors:d.anchor,train:d.training,main:shuffle(d.main),
                     refs:d.refs,attnGrid:d.attention_check,attnPos:d.attention_position,
                     attnGrid2:d.attention_check_2,attnPos2:d.attention_position_2,
                     instrGrid:d.instr_check_grid,instrHazard:d.instr_check_hazard,
                     totalPool:d.total_pool,remaining:d.remaining_under_5});
});

function shuffle(a){for(let i=a.length-1;i>0;i--){let j=Math.floor(Math.random()*(i+1));[a[i],a[j]]=[a[j],a[i]]}return a}

function R(){document.getElementById('app').innerHTML={
    briefing:Bf,anchor:Ba,practice:Bp,main:Bm,feedback:Bk,complete:Bc
}[S.phase]();bindE()}

function Bf(){
let p=S.page;
if(p===0)return `<div class="top"><h1>灾害风险感知评分任务</h1><div class="sub">重庆市街景图像 · 正式实验 v4</div></div>
<div class="card briefing">
<h2>任务概述</h2>
<p>您将看到<strong>20组</strong>重庆市不同位置的街景图像（每组4张，前/右/后/左四个方向）。</p>
<p>每组图像，您需要回答<strong>三个问题</strong>——只看图像中可以看到的东西，判断该位置在三种灾害情境下的风险程度。</p>
<p style="color:#888;font-size:.8em">这不是对错测试——不同人的判断可能不同，我们研究的就是这种差异。预计 20-25 分钟。</p>
${disasterCards()}
<h2>流程</h2>
<p><strong>第1步：</strong>浏览1张锚定示例图 → 了解1-5分"长什么样"</p>
<p><strong>第2步：</strong>练习评分4张 → 独立评分后查看对比</p>
<p><strong>第3步：</strong>正式评分15张 → 独立判断</p>
<p><strong>第4步：</strong>填写简短反馈 → 完成</p>
<div class="warn-box"><strong>⚠ 请使用1-5的整个范围</strong>。如果图像确实极端，就打1或5。不要全部集中在3分。</div>
</div>
<div class="pool-info">📊 总格网池: ${S.totalPool} | 待完成: ${S.remaining} | 您的15组正式评分将从待评格网中随机分配</div>
<div class="btns"><button class="btn btn-p" onclick="S.page=1;R()">下一页：评分指南 →</button></div>`;

return `<div class="top"><h1>评分线索指南</h1></div>
<div class="card briefing">
<h2>怎么看图评分？</h2>
<p>评分时<strong>只看图像中可见的物理场景</strong>——路面、建筑、山体、植被、道路宽度等。</p>
${cueTable()}
<h3>不能用的线索</h3>
<div class="warn-box">
✗ 天气好坏——假设暴雨/火灾已发生<br>
✗ 社区是否"高档"——与灾害风险无关<br>
✗ 地下排水/室内消防——你看不到它们<br>
✗ "总体安全感"——我们问的是具体灾害
</div>
<div style="background:#f0f2f5;padding:14px;border-radius:8px;margin-top:12px">
<h3 style="margin:0 0 8px">参与者信息</h3>
<div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px">
<div><label style="font-size:.8em;font-weight:600">编号</label><input id="pid" value="P01" style="width:100%;padding:8px;border:1px solid #dfe6e9;border-radius:5px"></div>
<div><label style="font-size:.8em;font-weight:600">年龄组</label><select id="age" style="width:100%;padding:8px;border:1px solid #dfe6e9;border-radius:5px"><option value="">请选择</option><option>18-25</option><option>26-35</option><option>36-45</option><option>46-55</option><option>56+</option></select></div>
<div><label style="font-size:.8em;font-weight:600">重庆居住年限</label><select id="cq" style="width:100%;padding:8px;border:1px solid #dfe6e9;border-radius:5px"><option value="">请选择</option><option>&lt;1年</option><option>1-5年</option><option>5-10年</option><option>&gt;10年</option><option>从未居住</option></select></div>
</div></div></div>
<div class="btns">
<button class="btn" style="background:#b2bec3;color:#fff" onclick="S.page=0;R()">← 返回</button>
<button class="btn btn-g" onclick="startTask()">开始评分任务 →</button>
</div>`;
}

function disasterCards(){
return `<div class="disaster-card"><h4>FLD · 洪涝积水</h4>
<p><strong>问题：</strong>如果发生暴雨，这个地方积水的可能性有多大？</p>
<p><strong>高分：</strong>大面积硬质路面、建筑紧逼峡谷感、道路低洼、无绿化</p>
<p><strong>低分：</strong>透水地面、空间开阔、位于坡顶、排水设施可见</p></div>
<div class="disaster-card"><h4>GEO · 边坡滑坡</h4>
<p><strong>问题：</strong>从街景来看，这个地方发生滑坡/崩塌的可能性有多大？</p>
<p><strong>高分：</strong>裸岩/土坡直接可见、挡土墙密集、道路紧贴陡坡脚</p>
<p><strong>低分：</strong>完全平坦、视野内无任何坡面 → 直接打1分</p></div>
<div class="disaster-card"><h4>FIR · 火灾疏散</h4>
<p><strong>问题：</strong>如果附近建筑发生火灾，疏散和消防车到达有多困难？</p>
<p><strong>高分：</strong>建筑密集无间距、巷道狭窄、占道停车</p>
<p><strong>低分：</strong>建筑稀疏、道路宽阔、附近有开阔场地</p></div>`;
}

function cueTable(){
return `<table style="width:100%;font-size:.83em;border-collapse:collapse;margin:8px 0">
<tr style="background:#f0f2f5"><th>灾种</th><th>看什么</th><th>高风险（→5分）</th><th>低风险（→1分）</th></tr>
<tr><td><strong>FLD 洪涝</strong></td><td><b>地面渗透性</b></td><td>硬质路面全覆盖、峡谷感、低洼、无绿化</td><td>透水地面、开阔、坡顶、排水可见</td></tr>
<tr><td><strong>GEO 边坡</strong></td><td><b>有无坡面</b></td><td>裸岩/土坡可见、挡土墙密集、路贴陡坡</td><td>完全平坦、无坡面 → <b>直接1分</b></td></tr>
<tr><td><strong>FIR 火灾</strong></td><td><b>道路通行性</b></td><td>建筑密集无间距、巷道窄、占道停车</td><td>建筑稀疏、道路宽、有开阔场地</td></tr>
</table>
<div style="margin-top:10px;padding:10px;background:#fff3cd;border-radius:6px;font-size:.78em">
<strong>⚠️ FLD vs FIR 怎么区分？</strong><br>
• FLD 看<b>地面</b>（能不能透水）→ 硬化路面积水，泥土地不积水<br>
• FIR 看<b>道路</b>（能不能跑出去）→ 窄巷子难疏散，宽阔道路容易疏散<br>
• 同一地方可以 FLD=高 FIR=低（宽阔硬化广场），也可以 FLD=低 FIR=高（窄巷透水砖路）
</div>`;
}

function Ba(){
let g=S.currentList[S.currentIdx],ref=S.refs[g]||{};
return `<div class="top"><h1>第1步：锚定浏览</h1><div class="sub">了解评分标准 · 不须作答</div></div>
<div class="steps"><span class="on">锚定</span><span>练习</span><span>正式评分</span></div>
<div style="text-align:center;color:#888;font-size:.8em;margin:6px 0">唯一锚定示例</div>
${imgGrid(g)}
<div class="card ref-box"><strong>AI 参考分（帮您建立1-5的参照）：</strong><br>
FLD 积水可能性 = <strong>${ref.FLD}/5</strong> &nbsp;|&nbsp;
GEO 滑坡可能性 = <strong>${ref.GEO}/5</strong> &nbsp;|&nbsp;
FIR 疏散难度 = <strong>${ref.FIR}/5</strong>
<div style="font-size:.75em;color:#888;margin-top:4px">请对照下方线索速查表理解每个分数的含义</div></div>
${cueToggle()}
<div class="btns"><button class="btn btn-g" onclick="nextAnchor()">进入练习 →</button></div>`;
}

function Bp(){
let g=S.currentList[S.currentIdx],prev=S.trainResults[S.currentIdx],pct=Math.round((S.currentIdx+1)/S.currentList.length*100);
return `<div class="top"><h1>第2步：练习校准</h1><div class="sub">独立评分后查看对比 · ${S.currentIdx+1}/${S.currentList.length}</div></div>
<div class="steps"><span>锚定</span><span class="on">练习</span><span>正式评分</span></div>
<div class="bar-wrap"><div class="bar-fill" style="width:${pct}%"></div></div>
${prev?feedbackBox(prev):''}
${imgGrid(g)}
${cueToggle()}
${ratingForm('practice')}
${scaleTracker()}
${fastWarn()}
<div class="btns"><button class="btn btn-p" id="sub" disabled onclick="subPractice()">${S.currentIdx<S.currentList.length-1?'提交→下一张':'提交→正式评分'}</button></div>`;
}

function Bm(){
let g=S.currentList[S.currentIdx],pct=Math.round((S.currentIdx+1)/S.currentList.length*100);
let acWarn='';
if(S.currentIdx===S.attnPos-1&&S.attnGrid)acWarn+='<div class="attn-warn show">注意：接下来的场景您之前已经见过。请根据当前判断评分。</div>';
if(S.currentIdx===S.attnPos2-1&&S.attnGrid2)acWarn+='<div class="attn-warn show">注意：接下来的场景您在本轮中已经评过。</div>';
if(S.instrPos!==undefined&&S.currentIdx===S.instrPos)acWarn+='<div class="attn-warn show" style="background:#dfe6e9">📋 本轮为指令检验，请仔细阅读问题文字。</div>';
return `<div class="top"><h1>第3步：正式评分</h1><div class="sub">独立判断 · 无反馈 · ${S.currentIdx+1}/${S.currentList.length}</div></div>
<div class="steps"><span>锚定</span><span>练习</span><span class="on">正式评分</span></div>
<div class="bar-wrap"><div class="bar-fill" style="width:${pct}%"></div></div>
${acWarn}
${imgGrid(g)}
${cueToggle()}
${ratingForm('main',(S.instrPos!==undefined&&S.currentIdx===S.instrPos)?S.instrHazard:null)}
${scaleTracker()}
${fastWarn()}
<div class="btns"><button class="btn btn-p" id="sub" disabled onclick="subMain()">${S.currentIdx<S.currentList.length-1?'提交→下一组':'提交→反馈问卷'}</button></div>`;
}

function Bk(){
return `<div class="top"><h1>反馈问卷</h1></div>
<div class="card fb-form">
<label>1. 三个问题分别在问什么？</label><textarea id="f1" rows="2"></textarea>
<label>2. 哪个最有把握？哪个最没把握？</label><textarea id="f2" rows="2"></textarea>
<label>3. 评分时主要看图像的哪些特征？</label><textarea id="f3" rows="2"></textarea>
<label>4. 有没有"看着舒服"但给了高风险，或反之？</label><textarea id="f4" rows="2"></textarea>
<label>5. 洪水风险和火灾疏散难度——如何区分？</label><textarea id="f5" rows="2"></textarea>
<label>6. 1-5分范围够用吗？</label><textarea id="f6" rows="1"></textarea>
<label>7. 评分标准前后有变化吗？</label><textarea id="f7" rows="1"></textarea>
<label>8. 有什么困惑？</label><textarea id="f8" rows="2"></textarea>
<label>9. 有什么建议？</label><textarea id="f9" rows="2"></textarea>
<div class="btns"><button class="btn btn-g" onclick="subFeedback()">提交并完成</button></div>
</div>`;
}

function Bc(){
return `<div class="done card"><h2>实验完成！</h2><p>感谢参与！数据已保存。</p><p style="color:#888;margin-top:8px;font-size:.85em">您可以关闭此页面了。</p></div>`;
}

function imgGrid(gid){
let dirs=['0','90','180','270'],labels=['前 (F)','右 (R)','后 (B)','左 (L)'];
return '<div class="imgs">'+dirs.map((d,i)=>`<div><img src="/img/${gid}/${d}" alt="${labels[i]}" loading="lazy"><span class="tag">${labels[i]}</span></div>`).join('')+'</div>';
}

function cueToggle(){
return `<div class="cue-toggle"><button onclick="document.getElementById('cuecard').classList.toggle('show')">📋 评分线索速查（点击展开/收起）</button></div>
<div class="cue-card" id="cuecard">${cueTable()}<p style="font-size:.72em;color:#888;margin-top:4px">1=最低 · 4=中等 · 7=最高 &nbsp;|&nbsp; 三个灾种独立判断</p></div>`;
}

function ratingForm(phase,instrHazard){
let scales={
  FLD:{q:'Q1. 如果发生暴雨，从街景来看，这个地方<b>积水</b>的可能性有多大？',
       tip:'1=极低 · 3=中等 · 5=极高',
       lbls:['极低\\n几乎不积水','较低\\n不太可能','中等\\n有可能','较高\\n很可能','极高\\n几乎必定']},
  GEO:{q:'Q2. 从街景来看，这个地方发生<b>滑坡/崩塌</b>的可能性有多大？',
       tip:'1=极低 · 3=中等 · 5=极高 · 看不到坡面→直接1分',
       lbls:['极低\\n完全无风险','较低\\n风险不大','中等\\n有风险','较高\\n风险较大','极高\\n极端风险']},
  FIR:{q:'Q3. 如果附近建筑发生火灾，从街景来看，<b>疏散和消防车</b>到达有多困难？',
       tip:'1=极低 · 3=中等 · 5=极高',
       lbls:['极低\\n极容易疏散','较低\\n较容易','中等\\n有一定难度','较高\\n较困难','极高\\n几乎无法疏散']}
};
if(instrHazard && scales[instrHazard]){
  scales[instrHazard].q='<span style="color:#e17055">【指令检验】本题请选4。</span> '+scales[instrHazard].q;
  scales[instrHazard].tip='请仔细阅读问题文字后作答 · '+scales[instrHazard].tip;
}
let h='';
for(let k of ['FLD','GEO','FIR']){
  let s=scales[k];
  h+=`<div class="q-block"><h3>${s.q}</h3><div class="tip">${s.tip}</div><div class="scale">`;
  for(let v=1;v<=5;v++)h+=`<label><input type="radio" name="q_${k}" value="${v}" onchange="chk()"><span class="dot">${v}</span><span class="lbl">${s.lbls[v-1]}</span></label>`;
  h+='</div></div>';
}
return h;
}

function getUsedValues(){
  let used={FLD:new Set(),GEO:new Set(),FIR:new Set()};
  S.ratings.forEach(r=>{['FLD','GEO','FIR'].forEach(k=>used[k].add(r[k]));});
  return used;
}
function scaleTracker(){
  let used=getUsedValues();
  let h='<div style="background:#f0f2f5;border-radius:8px;padding:10px 14px;margin:10px 0;font-size:.78em">';
  h+='<strong>📊 分值使用情况</strong>（请用满1-5）：';
  for(let k of ['FLD','GEO','FIR']){
    let bars='';
    for(let v=1;v<=5;v++){
      let c=used[k].has(v)?'#0984e3':'#dfe6e9';
      bars+=`<span style="display:inline-block;width:26px;height:20px;line-height:20px;text-align:center;
        background:${c};color:${used[k].has(v)?'#fff':'#b2bec3'};border-radius:3px;margin:1px;font-size:.75em">${v}</span>`;
    }
    h+=`<br>${k}: ${bars}`;
  }
  let totalUsed=new Set([...used.FLD,...used.GEO,...used.FIR]).size;
  if(totalUsed<4)h+='<br><span style="color:#e17055">⚠️ 你只用了'+totalUsed+'个不同分值，请尝试使用更极端的分数（1和5）</span>';
  h+='</div>';
  return h;
}
function fastWarn(){
  if(S.ratings.length<3)return'';
  let recent=S.ratings.slice(-3);
  let allFast=recent.every(r=>r.response_time_sec<8);
  if(allFast)return'<div style="background:#ffeaa7;border:1px solid #fdcb6e;padding:8px 14px;border-radius:6px;font-size:.8em;margin:8px 0">⚠️ 最近3题作答都很快（<8秒），请仔细观看每张图片后再评分。</div>';
  return'';
}
function feedbackBox(r){
let oks=[Math.abs(r.FLD-r.rFLD)<=1,Math.abs(r.GEO-r.rGEO)<=1,Math.abs(r.FIR-r.rFIR)<=1];
let n=oks.filter(x=>x).length,cls=n>=3?'good':(n>=2?'warn':'bad');
return `<div class="fb show ${cls}"><strong>${n>=3?'很好！':(n>=2?'部分偏差':'偏差较大')}</strong>
<div style="margin-top:3px;font-size:.83em">您的评分：FLD=<b>${r.FLD}</b>(参考${r.rFLD}) GEO=<b>${r.GEO}</b>(参考${r.rGEO}) FIR=<b>${r.FIR}</b>(参考${r.rFIR}) · 偏差≤1即视为一致</div></div>`;
}

function startTask(){
S.pid=document.getElementById('pid').value||'P01';
S.age=document.getElementById('age').value;S.cq=document.getElementById('cq').value;
S.phase='anchor';S.currentList=S.anchors;S.currentIdx=0;R();
}
function nextAnchor(){S.currentIdx++;if(S.currentIdx>=S.currentList.length){S.phase='practice';S.currentList=S.train;S.currentIdx=0;S.trainResults=Array(S.train.length).fill(null)}R();}
function chk(){let b=document.getElementById('sub');if(b)b.disabled=['FLD','GEO','FIR'].some(k=>!document.querySelector(`input[name="q_${k}"]:checked`));}
function getR(){return{FLD:parseInt(document.querySelector('input[name="q_FLD"]:checked')?.value||'0'),GEO:parseInt(document.querySelector('input[name="q_GEO"]:checked')?.value||'0'),FIR:parseInt(document.querySelector('input[name="q_FIR"]:checked')?.value||'0')};}
let _demoSent=false;
function saveOne(phase,grid_id,r,is_ac){
  let payload={participant_id:S.pid,phase,grid_id,FLD:r.FLD,GEO:r.GEO,FIR:r.FIR,
               response_time_sec:Math.round((Date.now()-S.qStart)/100)/10,
               is_ac:is_ac?'1':'',is_ac2:'',is_instr:'',instr_hazard:'',instr_passed:null};
  if(!_demoSent){payload.demographics={age:S.age,chongqing_years:S.cq};_demoSent=true}
  fetch('/save-one',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload),keepalive:true}).catch(()=>{});
}
function subPractice(){
let r=getR();if(!r.FLD||!r.GEO||!r.FIR)return;
let g=S.currentList[S.currentIdx],rt=(Date.now()-S.qStart)/1000,ref=S.refs[g]||{FLD:4,GEO:4,FIR:4};
S.trainResults[S.currentIdx]={...r,rFLD:ref.FLD,rGEO:ref.GEO,rFIR:ref.FIR};
S.ratings.push({phase:'practice',grid_id:g,...r,response_time_sec:rt,is_ac:''});
saveOne('practice',g,r,false);
S.currentIdx++;
if(S.currentIdx>=S.currentList.length){let m=[...S.main];
  if(S.attnGrid&&S.attnPos<=m.length)m.splice(S.attnPos,0,S.attnGrid);
  if(S.attnGrid2&&S.attnPos2<=m.length)m.splice(S.attnPos2,0,S.attnGrid2);
  if(S.instrGrid){let ip=Math.min(8,m.length);m.splice(ip,0,S.instrGrid);S.instrPos=ip;}
  S.currentList=m;S.currentIdx=0;S.phase='main';}
R();S.qStart=Date.now();
}
function subMain(){
let r=getR();if(!r.FLD||!r.GEO||!r.FIR)return;
let g=S.currentList[S.currentIdx],rt=(Date.now()-S.qStart)/1000;
let isAC1=(g===S.attnGrid&&S.currentIdx===S.attnPos);
let isAC2=(g===S.attnGrid2&&S.currentIdx===S.attnPos2);
let isInstr=(S.instrPos!==undefined&&S.currentIdx===S.instrPos);
let isAC=isAC1||isAC2||isInstr;
let instrPassed=null;
if(isInstr&&S.instrHazard){instrPassed=r[S.instrHazard]===4;}
S.ratings.push({phase:'main',grid_id:g,...r,response_time_sec:rt,
  is_ac:isAC?'1':'',is_ac2:isAC2?'1':'',is_instr:isInstr?'1':'',
  instr_hazard:isInstr?S.instrHazard:'',instr_passed:instrPassed});
saveOne('main',g,r,isAC);
S.currentIdx++;if(S.currentIdx>=S.currentList.length)S.phase='feedback';
R();S.qStart=Date.now();
}
async function subFeedback(){
let fb={};for(let i=1;i<=9;i++)fb['Q'+i]=document.getElementById('f'+i)?.value||'';
let pracR=S.trainResults[1],mainR=S.ratings.filter(r=>r.grid_id===S.attnGrid&&r.phase==='main')[0];
let ac1Passed=null;
if(pracR&&mainR)ac1Passed=[Math.abs(pracR.FLD-mainR.FLD),Math.abs(pracR.GEO-mainR.GEO),Math.abs(pracR.FIR-mainR.FIR)].every(d=>d<=2);
let ac2Rows=S.ratings.filter(r=>r.is_ac2==='1');
let ac2Passed=null;
if(ac2Rows.length===2){let a=ac2Rows[0],b=ac2Rows[1];
  ac2Passed=[Math.abs(a.FLD-b.FLD),Math.abs(a.GEO-b.GEO),Math.abs(a.FIR-b.FIR)].every(d=>d<=2);}
let instrRow=S.ratings.filter(r=>r.is_instr==='1')[0];
let instrPassed=instrRow?instrRow.instr_passed:null;
let payload={participant_id:S.pid,demographics:{age:S.age,chongqing_years:S.cq},feedback:fb,
  ac1_passed:ac1Passed,ac2_passed:ac2Passed,instr_passed:instrPassed,n_ratings:S.ratings.length};
try{
  let resp=await fetch('/save',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload),keepalive:true});
  let d=await resp.json();
  if(d.status==='ok')S.phase='complete';
}catch(e){}
R();
}
function bindE(){S.qStart=Date.now();if(['practice','main'].includes(S.phase))chk();}
R();
</script>
</body>
</html>'''

ADMIN_HTML = r'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>数据管理 — 感知评分实验</title>
<style>
body{font-family:-apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif;max-width:700px;margin:40px auto;padding:20px;background:#f5f5f5}
h1{color:#2c3e50}
table{width:100%;border-collapse:collapse;background:#fff;border-radius:8px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,.1)}
th{background:#2c3e50;color:#fff;padding:10px 14px;text-align:left}
td{padding:10px 14px;border-bottom:1px solid #eee}
a{color:#0984e3;text-decoration:none}
a:hover{text-decoration:underline}
.refresh{margin:12px 0;display:inline-block;padding:8px 18px;background:#0984e3;color:#fff;border-radius:6px;text-decoration:none;font-size:.9em}
</style>
</head>
<body>
<h1>感知评分数据管理</h1>
{{PROGRESS}}
<p style="color:#888">参与者提交评分后，数据文件会出现在下方。点击 Download 下载 CSV（含经纬度和图片编号）。</p>
<a class="refresh" href="/admin">刷新</a>
<table><tr><th>文件名</th><th>数据量</th><th>操作</th></tr>{{ROWS}}</table>
<p style="color:#888;margin-top:20px;font-size:.8em">注意：Render 免费版重新部署后数据会丢失。请及时下载保存。</p>
</body>
</html>'''

def main():
    # Ensure data files are in DATA_DIR
    for fname in ['perception_rating_sample_v1.csv', 'perception_rating_image_list_v1.csv']:
        src = Path('clean') / fname
        dst = DATA_DIR / fname
        if src.exists() and not dst.exists():
            import shutil
            shutil.copy2(str(src), str(dst))

    port = int(os.environ.get('PORT', 8724))
    server = HTTPServer(('0.0.0.0', port), Handler)
    server.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    print(f"Server running on port {port}")
    print(f"Grid pool: {len(ALL_GRIDS_POOL)} | Anchor: {ANCHOR_GRID} | Practice: {len(PRACTICE_GRIDS)}")
    counts = load_counts()
    rated = sum(1 for c in counts.values() if c > 0)
    done = sum(1 for c in counts.values() if c >= 5)
    print(f"Progress: {rated}/400 rated | {done}/400 complete (≥5)")
    server.serve_forever()

if __name__ == '__main__':
    main()
