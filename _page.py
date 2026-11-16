# -*- coding: utf-8 -*-
"""连接游戏页面(9222), 安装应用层探针:
   1. JSON.parse 钩子: 捕获含 llm/link/pid 的解析串
   2. 检查页面渲染方式(canvas/DOM) 与 socket 列表
"""
from playwright.sync_api import sync_playwright

HOOK = """
(function(){
  if (window.__tw_chat_probe) return 'already';
  window.__tw_chat_probe = true;
  window.__tw_chat_json = window.__tw_chat_json || [];
  const op = JSON.parse;
  JSON.parse = function(s){
    try{
      if (typeof s === 'string' && s.length < 20000 &&
          (s.indexOf('llm') >= 0 || s.indexOf('"link"') >= 0 || s.indexOf('"\\"pid\\"') >= 0)) {
        if (window.__tw_chat_json.length < 200) window.__tw_chat_json.push(s);
      }
    }catch(e){}
    return op.apply(this, arguments);
  };
  return 'installed';
})();
"""

CHECK = """
(function(){
  const cv = document.querySelectorAll('canvas').length;
  const sockets = (window.__tw_sockets||[]).map(s=>s.url);
  return {canvas: cv, hooked: !!window.__tw_chat_probe,
          nchat: (window.__tw_chat_json||[]).length,
          sockets: sockets};
})();
"""

with sync_playwright() as p:
    browser = p.chromium.connect_over_cdp("http://127.0.0.1:9222")
    ctx = browser.contexts[0]
    pages = [pg for pg in ctx.pages if "rivergame" in pg.url]
    if not pages:
        print("[!] 未找到游戏页"); raise SystemExit
    page = pages[0]
    r = page.evaluate(HOOK)
    print("hook:", r)
    c = page.evaluate(CHECK)
    print("page:", c)
