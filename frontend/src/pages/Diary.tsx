import { useEffect, useState } from "react";
import { api } from "@/api/client";
import type { DiaryRecord } from "@/api/types";
import { RelationshipPicker, useRelationships } from "@/components/RelationshipPicker";
import { Alert, Badge, Card, Empty, ErrorBox, Field, Spinner } from "@/components/ui";
import { useFlow } from "@/store";

const KINDS: [string, string][] = [
  ["fact", "发生的事"],
  ["plan", "计划"],
  ["feeling", "感受"],
];

export function DiaryPage() {
  const [entries, setEntries] = useState<DiaryRecord[] | null>(null);
  const [err, setErr] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const [text, setText] = useState("");
  const [kind, setKind] = useState("fact");
  const [flow] = useFlow();
  const relationship = flow.relationshipId;
  const [rels] = useRelationships();
  const relName = (id: string) => rels?.find((r) => r.id === id)?.name || id;
  const [query, setQuery] = useState("");
  const [found, setFound] = useState<{ entries: DiaryRecord[]; dropped: Record<string, number>; enabled: boolean } | null>(null);
  const [strict, setStrict] = useState<Record<string, { text: string; note: string } | string>>({});
  const [memoryOn, setMemoryOn] = useState<boolean | null>(null);
  const [editing, setEditing] = useState<string | null>(null);
  const [draft, setDraft] = useState<{ text: string; approved_clause: string }>({ text: "", approved_clause: "" });

  async function load() {
    const r = await api.get<{ entries: DiaryRecord[]; memory_enabled_by_default: boolean }>("/diary");
    setEntries(r.entries);
    setMemoryOn(r.memory_enabled_by_default);
  }
  useEffect(() => {
    void load().catch(setErr);
  }, []);

  async function create() {
    setBusy(true);
    setErr(null);
    try {
      await api.post("/diary", { text, kind, allowed_relationships: relationship ? [relationship] : [] });
      setText("");
      await load();
    } catch (e) {
      setErr(e);
    } finally {
      setBusy(false);
    }
  }

  async function search() {
    setErr(null);
    try {
      const params = new URLSearchParams({ q: query, relationship_id: relationship });
      setFound(await api.get(`/diary/search?${params.toString()}`));
    } catch (e) {
      setErr(e);
    }
  }

  async function strictReply(id: string) {
    try {
      const out = (await api.post(`/diary/${id}/strict-reply?relationship_id=${encodeURIComponent(relationship)}`)) as { text: string; note: string };
      setStrict((prev) => ({ ...prev, [id]: out }));
    } catch (e) {
      setStrict((prev) => ({ ...prev, [id]: (e as Error).message }));
    }
  }

  if (!entries) return err ? <ErrorBox error={err} /> : <Spinner />;

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-xl font-semibold">个人日记</h1>
        <p className="text-sm text-ink-2 mt-1">
          日记只保存在本机，不进入报告、导出与诊断信息。它只有在<strong>你授权的关系</strong>里、且<strong>开关打开</strong>时才可能被检索到；
          写进草稿的日记句子必须是你批准的原文，并会标注来源与版本。
        </p>
      </div>

      <Card title="记忆开关">
        <label className="flex items-center gap-2 text-sm">
          <input type="checkbox" checked={memoryOn ?? true} disabled={memoryOn === null} onChange={async (e) => { try { await api.put(`/diary/memory/state?enabled=${e.target.checked}`); await load(); } catch (er) { setErr(er); } }} />
          允许代回复读取日记（关闭时检索器根本不会被调用）
        </label>
      </Card>

      <Card title="写一条">
        <div className="space-y-3">
          <Field label="内容">
            <textarea className="input min-h-[80px]" value={text} onChange={(e) => setText(e.target.value)} placeholder="今天发生了什么、感受如何，或者接下来想做什么" />
          </Field>
          <div className="grid gap-3 sm:grid-cols-3">
            <Field label="类型" hint="计划不会被写成「已经发生」">
              <select className="input" value={kind} onChange={(e) => setKind(e.target.value)}>
                {KINDS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
              </select>
            </Field>
            <RelationshipPicker label="可用于哪个对象" hint="这条日记只会在该对象的草稿里被检索到。" />
            <div className="flex items-end">
              <button className="btn btn-primary" disabled={busy || !text.trim()} onClick={create}>保存</button>
            </div>
          </div>
        </div>
      </Card>

      <Card title={`记忆检索（当前对象：${relName(relationship)}）`}>
        <div className="flex flex-wrap items-end gap-2">
          <Field label="查询（模拟「当前聊到的话题」）">
            <input className="input w-72" value={query} onChange={(e) => setQuery(e.target.value)} placeholder="例如 周末 一起 吃饭" />
          </Field>
          <button className="btn btn-ghost" onClick={search} disabled={!query.trim()}>检索</button>
          <span className="text-xs text-ink-3">检索是词法的（向量检索在后续阶段），所以用词要接近日记原文。</span>
        </div>
        {found && (
          <div className="mt-3 space-y-2">
            {found.enabled ? null : <Alert tone="warn">记忆开关当前关闭：检索器根本不会被调用。</Alert>}
            {found.entries.length === 0 ? <Empty>没有可用于该关系的日记</Empty> : (
              <ul className="text-sm space-y-1">
                {found.entries.map((e) => (
                  <li key={e.id}>· [{KINDS.find(([v]) => v === e.kind)?.[1]}] {e.text} <span className="text-xs text-ink-3 mono">v{e.revision}</span></li>
                ))}
              </ul>
            )}
            {Object.keys(found.dropped).length > 0 && (
              <p className="text-xs text-ink-3">被丢弃：{Object.entries(found.dropped).map(([k, v]) => `${k}×${v}`).join("，")}</p>
            )}
          </div>
        )}
      </Card>

      <Card title={`日记（${entries.length}）`}>
        {entries.length === 0 ? <Empty>还没有日记</Empty> : (
          <ul className="space-y-2">
            {entries.map((e) => (
              <li key={e.id} className="rounded-lg border border-line p-3 text-sm">
                <div className="flex flex-wrap items-center gap-2">
                  <Badge>{KINDS.find(([v]) => v === e.kind)?.[1] ?? e.kind}</Badge>
                  <span className="mono text-xs text-ink-3">v{e.revision}</span>
                  {e.enabled ? <Badge tone="good">参与检索</Badge> : <Badge tone="warn">已关闭</Badge>}
                  {e.allowed_relationships.length === 0 ? <Badge tone="warn">未授权任何对象</Badge> : <Badge>{e.allowed_relationships.map(relName).join("、")}</Badge>}
                  {e.approved_clause ? <Badge tone="info">已批准短句</Badge> : null}
                </div>
                <p className="mt-1">{e.text}</p>
                <div className="mt-2 flex flex-wrap gap-2">
                  <button className="btn btn-ghost py-0.5 text-xs" onClick={async () => { try { await api.post(`/diary/${e.id}/enabled?enabled=${!e.enabled}`); await load(); } catch (er) { setErr(er); } }}>
                    {e.enabled ? "关闭这条" : "启用这条"}
                  </button>
                  <button className="btn btn-ghost py-0.5 text-xs" onClick={() => { setEditing(editing === e.id ? null : e.id); setDraft({ text: e.text, approved_clause: e.approved_clause ?? "" }); }}>{editing === e.id ? "收起编辑" : "编辑 / 批准短句"}</button>
                  <button className="btn btn-ghost py-0.5 text-xs" onClick={() => strictReply(e.id)} title="需要先批准一句原文短句">严格模式话术</button>
                  <button className="btn btn-danger py-0.5 text-xs" onClick={async () => { try { await api.del(`/diary/${e.id}`); await load(); } catch (er) { setErr(er); } }}>删除</button>
                </div>
                {editing === e.id && (
                  <div className="mt-2 space-y-2 rounded-lg bg-paper p-2">
                    <Field label="正文（修改会提升版本号，旧版本立即不可检索）">
                      <textarea className="input min-h-[60px]" value={draft.text} onChange={(ev) => setDraft({ ...draft, text: ev.target.value })} />
                    </Field>
                    <Field label="批准用于话术的短句（必须逐字出现在正文里；严格模式只会原样使用它）">
                      <input className="input" value={draft.approved_clause} onChange={(ev) => setDraft({ ...draft, approved_clause: ev.target.value })} placeholder="从正文里复制一句" />
                    </Field>
                    <div className="flex gap-2">
                      <button className="btn btn-primary py-0.5 text-xs" onClick={async () => { setErr(null); try { await api.patch(`/diary/${e.id}`, { text: draft.text !== e.text ? draft.text : undefined, approved_clause: draft.approved_clause || undefined }); setEditing(null); await load(); } catch (er) { setErr(er); } }}>保存</button>
                      {e.approved_clause && <button className="btn btn-ghost py-0.5 text-xs" onClick={async () => { try { await api.patch(`/diary/${e.id}`, { approved_clause: "" }); await load(); } catch (er) { setErr(er); } }}>撤销批准</button>}
                    </div>
                  </div>
                )}
                {strict[e.id] && (
                  typeof strict[e.id] === "string" ? <p className="text-warn text-xs mt-1">{strict[e.id] as string}</p> : (
                    <p className="text-xs mt-1">严格模式：「{(strict[e.id] as { text: string }).text}」</p>
                  )
                )}
              </li>
            ))}
          </ul>
        )}
        <div className="mt-3">
          <button className="btn btn-danger" onClick={async () => { if (confirm("清空全部日记？")) { await api.del("/diary"); await load(); } }}>清空全部日记</button>
        </div>
      </Card>
      <ErrorBox error={err} />
    </div>
  );
}
