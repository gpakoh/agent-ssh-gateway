"use strict";
(() => {
  const $ = (id) => document.getElementById(id);
  const names = {open:"Входящие",accepted:"В работе",waiting_review:"Ожидает проверки",resolved:"Решено"};
  const status = $("status"), error = $("error"), partial = $("partial"), columns = $("columns");
  const params = new URLSearchParams(location.search);
  const chosen = params.get("project");
  if (chosen && /^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$/.test(chosen)) $("project").value = chosen;
  let origin = null, project = $("project").value;

  function safeIssueUrl(raw) {
    if (!origin || typeof raw !== "string") return null;
    try {
      const url = new URL(raw);
      if (url.protocol !== "https:" || url.origin !== origin) return null;
      if (!/^\/[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+\/issues\/[0-9]+$/.test(url.pathname)) return null;
      return url.href;
    } catch { return null; }
  }
  function text(tag, content, cls) {
    const el = document.createElement(tag);
    if (cls) el.className = cls;
    el.textContent = String(content ?? "");
    return el;
  }
  async function json(url) {
    const response = await fetch(url, {credentials:"same-origin",headers:{"Accept":"application/json"},cache:"no-store"});
    if (!response.ok) {
      let reason = "";
      try { const body = await response.json(); if (typeof body.detail === "string") reason = body.detail; } catch {}
      throw new Error(reason || "HTTP " + response.status);
    }
    return response.json();
  }
  function fail(message) {error.hidden=false;error.textContent=message;}
  function loading(value) {$("refresh").disabled=value;$("project-form").querySelector('button[type="submit"]').disabled=value;}
  async function openThread(number, href) {
    $("discussion").hidden=true; status.textContent="Загрузка обсуждения…";
    try {
      const data = await json("/api/coordination/thread?"+new URLSearchParams({project,issue_number:String(number)}));
      const thread = data.thread;
      $("thread-title").textContent=thread.title || ("Issue #"+number);
      const list=$("thread-history");list.replaceChildren();
      for (const item of thread.history || []) {
        const entry=text("div","","entry");
        entry.append(text("div",(item.author || "Gitea")+" · "+(item.state || "комментарий")+" · "+(item.created_at || ""),"meta"));
        entry.append(text("div",item.body || ""));
        list.append(entry);
      }
      if (!list.children.length) list.append(text("div","Комментариев пока нет.","empty"));
      const link=$("gitea-link"), valid=safeIssueUrl(href || thread.url);
      link.hidden=!valid;if(valid)link.href=valid;else link.removeAttribute("href");
      $("discussion").hidden=false;$("discussion").scrollIntoView({behavior:"smooth",block:"start"});
      status.textContent="";
    } catch (e) {status.textContent="";fail("Не удалось открыть обсуждение: "+e.message);}
  }
  function draw(data) {
    columns.replaceChildren();origin=data.source_origin;
    for (const col of data.columns || []) {
      const section=text("section","","column");
      const header=text("div","","column-header");
      header.append(text("span",names[col.state] || col.label || col.state),text("span",col.count || 0,"count"));
      section.append(header);
      const list=text("div","","cards");
      for (const card of col.cards || []) {
        const btn=text("button","","card");btn.type="button";
        btn.append(text("span",card.title || "Issue #"+card.number,"card-title"));
        btn.append(text("span","#"+card.number+" · "+(card.source_project || "неизвестный проект"),"meta"));
        btn.addEventListener("click",()=>openThread(card.number,card.url));list.append(btn);
      }
      if (!list.children.length) list.append(text("div","Нет обращений","empty"));
      section.append(list);columns.append(section);
    }
    partial.hidden=!data.truncated;
    status.textContent=String(data.count || 0)+" обращений"+(data.truncated?" · частичная выборка":"");
  }
  async function load() {
    error.hidden=true;$("discussion").hidden=true;loading(true);status.textContent="Чтение Gitea…";
    project=$("project").value.trim();
    try {
      const data=await json("/api/coordination/board?"+new URLSearchParams({project,limit:"25"}));
      history.replaceState(null,"","/coordination?"+new URLSearchParams({project}));
      draw(data);
    } catch(e) {columns.replaceChildren();partial.hidden=true;status.textContent="Ошибка";fail(e.message);}
    finally {loading(false);}
  }
  $("project-form").addEventListener("submit",(event)=>{event.preventDefault();load();});
  $("refresh").addEventListener("click",load);
  $("close-thread").addEventListener("click",()=>$("discussion").hidden=true);
  load();
})();
