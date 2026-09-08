"""Build a local, interactive comparison from independently captured guide and Godot pages."""
import json
from pathlib import Path
from capture_guide_review import PAGES

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "docs/design/verification/guide-alignment"
NAMES = ["进入游戏", "农场对话", "旧工具演示", "手动比较", "升级委托", "工坊对话",
         "数据配对实验", "分级动作实验", "理解汇总", "代码卷轴", "分层提示", "行动验证",
         "失败复盘", "Bug 反例", "修改提案", "目标完成", "世界反馈", "成长档案",
         "技能解锁", "获得浇水器", "下一关预告"]
pages = [{"id": key, "name": name, "state": PAGES[key]} for key, name in zip(PAGES, NAMES)]
html = r'''<!doctype html>
<html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>作物适配浇水器 · 网页原型与 Godot 实机对照</title><link rel="icon" href="data:,">
<style>
*{box-sizing:border-box}body{margin:0;background:#13271e;color:#f6edd6;font:16px/1.6 system-ui,sans-serif}header{padding:20px 28px;background:#20392b}h1{font-size:24px;margin:0}p{margin:8px 0;color:#c9d3c4}.toolbar{display:flex;gap:16px;align-items:center;flex-wrap:wrap}select,button{font:inherit;padding:7px 12px;background:#fff5de;color:#213b2c;border:0;border-radius:5px}button{cursor:pointer}main{padding:18px 28px}.stage{position:relative;max-width:1280px;margin:16px auto;aspect-ratio:16/9;overflow:hidden;background:#000}.stage img{width:100%;height:100%;position:absolute;inset:0}.stage #actual{opacity:.5}.badge{position:absolute;bottom:10px;left:10px;padding:4px 12px;background:#13271edf;z-index:2}input[type=range]{width:260px}nav{display:flex;gap:8px;flex-wrap:wrap;margin:16px 0}nav button{font-size:13px;background:#304d3b;color:#f6edd6}nav button[aria-current=true]{background:#e3b750;color:#182d20}footer{font-size:14px;color:#b9c9b6}a{color:#edcc7e}
</style>
<header><h1>网页原型 ↔ Godot 实机</h1><p>以《美术资源与前端对接使用指南》内的交互演示为基准。21 个页面，同一 1280 × 720 视口。</p>
<div class="toolbar"><select id="picker" aria-label="选择页面"></select><button id="source">只看网页原型</button><button id="runtime">只看实机</button><label>实机叠加透明度 <input id="opacity" type="range" min="0" max="100" value="50"></label><output id="value">50%</output></div></header>
<main><nav id="pages" aria-label="全部页面"></nav><div class="stage"><img id="reference" alt="使用指南网页原型"><img id="actual" alt="Godot 实机截图"><span class="badge" id="badge"></span></div>
<p id="state"></p><footer>网页截图：交互演示的静止状态；实机截图：Godot 4.7.1 实际渲染的本地场景。动画帧、系统字体栅格化、真实运行结果文案会有所不同；截图不代表后端写入或正式回执已验证。S08 的实机展示无失败记录时的目标提示，网页为 L0 示例。<br><a href="captures.json">逐页渲染记录</a> · <a href="../guide-alignment-review.md">修正与验证说明</a></footer></main>
<script>
const pages=__PAGES__;
const picker=document.getElementById('picker'),actual=document.getElementById('actual'),reference=document.getElementById('reference'),opacity=document.getElementById('opacity');
for(const p of pages){picker.add(new Option(p.id+' · '+p.name,p.id));const b=document.createElement('button');b.textContent=p.id+' '+p.name;b.dataset.id=p.id;b.onclick=()=>show(p.id);document.getElementById('pages').append(b)}
function show(id){const p=pages.find(p=>p.id===id)||pages[0];picker.value=p.id;reference.src='reference-'+p.id+'.png';actual.src=p.id+'.png';document.getElementById('badge').textContent=p.id+' · '+p.name;document.getElementById('state').textContent='实机状态：'+p.state;document.querySelectorAll('nav button').forEach(b=>b.setAttribute('aria-current',String(b.dataset.id===p.id)));history.replaceState(null,'','#'+p.id)}
function blend(v){opacity.value=v;actual.style.opacity=v/100;document.getElementById('value').textContent=v+'%'}
picker.onchange=()=>show(picker.value);opacity.oninput=()=>blend(opacity.value);document.getElementById('source').onclick=()=>blend(0);document.getElementById('runtime').onclick=()=>blend(100);show(location.hash.slice(1));
</script></html>'''
OUTPUT.mkdir(parents=True, exist_ok=True)
(OUTPUT / "index.html").write_text(html.replace("__PAGES__", json.dumps(pages, ensure_ascii=False)), encoding="utf-8")
print(OUTPUT / "index.html")
