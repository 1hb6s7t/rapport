import { useEffect, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api, ApiError } from "@/api/client";
import type { ImportView, Message } from "@/api/types";
import { RelationshipPicker } from "@/components/RelationshipPicker";
import { Alert, Badge, Button, Card, ErrorBox, Field, Spinner } from "@/components/ui";
import { rememberWechatBinding } from "@/lib/wechat";
import { useFlow } from "@/store";

interface Config { enabled: boolean; available: boolean; connected: string | null; note: string }
interface Account { id: string; label: string; display_label?: string; nickname?: string; active?: boolean }
interface Friend { id: string; nickname: string; remark: string; alias: string; display_name: string }
interface Directory { account: string; contacts: Friend[]; total: number; accounts: Account[] }
interface Snapshot {
  snapshot_id: string; revision: string; account: string; contact: string; contact_id: string; relationship_id: string;
  messages: Message[]; warnings: { code: string; msg: string }[]; added?: number;
  voice_transcription?: { total: number; completed: number; unavailable: number; pending: number };
}
interface Job { status: string; result: Snapshot | null; error: { code: string; msg: string; hint: string } | null; progress?: { phase: string; voice_done?: number; voice_total?: number } }

export function DshReader() {
  const [flow, setFlow] = useFlow();
  const nav = useNavigate();
  const [config, setConfig] = useState<Config | null>(null);
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [account, setAccount] = useState("");
  const [contact, setContact] = useState("");
  const [contactId, setContactId] = useState("");
  const [directory, setDirectory] = useState<Directory | null>(null);
  const [contactsLoading, setContactsLoading] = useState(false);
  const [contactsError, setContactsError] = useState<unknown>(null);
  const [contactsVersion, setContactsVersion] = useState(0);
  const [friendQuery, setFriendQuery] = useState("");
  const [limit, setLimit] = useState(100);
  const [transcribeVoice, setTranscribeVoice] = useState(true);
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState<unknown>(null);
  const [autoSync, setAutoSync] = useState(false);
  const [page, setPage] = useState(0);
  const job = useRef<string | null>(null);
  const live = useRef(true);
  const key = JSON.stringify([flow.relationshipId, account, contactId, contact]);
  const current = useRef(key); current.current = key;
  const runRef = useRef<() => void>(() => {});
  const storageKey = `rapport_dsh_snapshot_${flow.relationshipId}`;
  const scope = { snapshot_id: snapshot?.snapshot_id, revision: snapshot?.revision, relationship_id: flow.relationshipId };

  async function loadAccounts(refresh = false) {
    const data = await api.get<{ accounts: Account[] }>(`/wechat/dsh/accounts${refresh ? "?refresh=true" : ""}`);
    if (!live.current) return;
    setAccounts(data.accounts);
    setAccount(value => {
      if (data.accounts.some(item => item.id === value)) return value;
      const active = data.accounts.filter(item => item.active);
      return active.length === 1 ? active[0].id : data.accounts.length === 1 ? data.accounts[0].id : "";
    });
  }

  useEffect(() => {
    live.current = true;
    let active = true;
    setSnapshot(null); setAutoSync(false);
    api.get<Config>("/wechat/dsh/config").then(async value => {
      if (!active) return;
      setConfig(value);
      if (value.enabled && value.available) {
        await loadAccounts();
        try {
          const saved = sessionStorage.getItem(storageKey);
          if (saved) {
            const previous = await api.post<Snapshot>("/wechat/dsh/snapshot", JSON.parse(saved));
            if (active) { setAccount(previous.account); setContact(previous.contact); setContactId(previous.contact_id); setSnapshot(previous); }
          }
        } catch { /* A removed/changed snapshot requires a new explicit read. */ }
      }
    }).catch(value => { if (active) setError(value); });
    return () => { active = false; live.current = false; if (job.current) void api.post(`/wechat/dsh/jobs/${job.current}/cancel`, {}).catch(() => {}); };
  }, [storageKey]);

  useEffect(() => {
    let active = true;
    setDirectory(null); setContactsError(null);
    if (!account || !config?.enabled || !config.available) { setContactsLoading(false); return; }
    setContactsLoading(true);
    api.post<Directory>("/wechat/dsh/contacts", { account }).then(value => {
      if (!active) return;
      if (value.account !== account) throw new Error("好友列表账号不匹配，请重新选择。");
      setDirectory(value);
      setAccounts(previous => previous.map(item => value.accounts.find(updated => updated.id === item.id) || item));
      setContactId(selected => value.contacts.some(friend => friend.id === selected) ? selected : "");
    }).catch(value => { if (active) setContactsError(value); })
      .finally(() => { if (active) setContactsLoading(false); });
    return () => { active = false; };
  }, [account, config?.enabled, config?.available, contactsVersion]);

  const friends = directory?.account === account ? directory.contacts : [];
  const selectedFriend = friends.find(friend => friend.id === contactId);
  const search = friendQuery.trim().toLocaleLowerCase();
  const filteredFriends = friends.filter(friend => !search || [friend.remark, friend.nickname, friend.alias, friend.id]
    .some(value => value.toLocaleLowerCase().includes(search)));

  useEffect(() => {
    if (selectedFriend) setContact(selectedFriend.display_name);
  }, [selectedFriend]);

  useEffect(() => {
    if (snapshot && (snapshot.account !== account || snapshot.contact_id !== contactId || snapshot.contact !== contact || snapshot.relationship_id !== flow.relationshipId)) {
      setSnapshot(null); setAutoSync(false); setPage(0);
    }
    if (job.current) void api.post(`/wechat/dsh/jobs/${job.current}/cancel`, {}).catch(() => {});
  }, [key]);

  useEffect(() => {
    if (!autoSync) return;
    const timer = setInterval(() => runRef.current(), 5000);
    return () => clearInterval(timer);
  }, [autoSync]);

  async function read(incremental: boolean) {
    if (busy || job.current || contactsLoading || !selectedFriend || !account || !contact.trim() || (incremental && !snapshot)) return;
    const expected = key;
    setBusy(incremental ? "正在同步新消息…" : "正在连接本机微信并读取历史…"); setError(null);
    try {
      const started = await api.post<{ id: string }>("/wechat/dsh/read", {
        account, contact: contact.trim(), relationship_id: flow.relationshipId, limit,
        contact_id: contactId,
        snapshot_id: incremental ? snapshot?.snapshot_id : null,
        transcribe_voice: transcribeVoice,
      });
      job.current = started.id;
      while (live.current && current.current === expected) {
        const state = await api.get<Job>(`/wechat/dsh/jobs/${started.id}`);
        if (!live.current || current.current !== expected) break;
        if (state.progress?.phase === "transcribing") {
          setBusy(`正在本地转写语音：${state.progress.voice_done ?? 0} / ${state.progress.voice_total ?? 0} 条…`);
        }
        if (state.status !== "running") {
          if (state.error) throw new ApiError(409, state.error.code, state.error.msg, state.error.hint);
          if (state.result) {
            setSnapshot(state.result); setPage(0);
            sessionStorage.setItem(storageKey, JSON.stringify({ snapshot_id: state.result.snapshot_id, revision: state.result.revision, relationship_id: flow.relationshipId }));
          }
          break;
        }
        await new Promise(resolve => setTimeout(resolve, 1000));
      }
      if (!live.current || current.current !== expected) await api.post(`/wechat/dsh/jobs/${started.id}/cancel`, {});
    } catch (value) { if (live.current && current.current === expected) { setError(value); setAutoSync(false); } }
    finally { job.current = null; if (live.current) setBusy(""); }
  }
  runRef.current = () => { if (snapshot && config?.enabled && !busy) void read(true); };

  async function useMaterial(reply: boolean) {
    if (!snapshot) return;
    const expected = key;
    setAutoSync(false); setBusy("导入材料…"); setError(null);
    try {
      const material = await api.post<ImportView>("/wechat/dsh/import", scope);
      if (!live.current || current.current !== expected) return;
      setFlow({ importId: material.id, analysisId: null, reportId: null });
      // Binding is explicit and optional: historical reading works while the
      // desktop is obscured. The reply page can still generate copyable drafts.
      nav(reply ? "/review?next=replies" : "/review");
    } catch (value) { setError(value); } finally { setBusy(""); }
  }

  async function bindForReply(visual = false) {
    if (!snapshot) return;
    const expected = key;
    setAutoSync(false); setBusy("正在核对微信窗口并绑定回复…"); setError(null);
    try {
      const material = await api.post<ImportView>("/wechat/dsh/import", scope);
      if (!live.current || current.current !== expected) return;
      const binding = await api.post<{ token: string; import_id: string; relationship_id: string }>("/wechat/dsh/bind", { ...scope, visual });
      if (!live.current || current.current !== expected) return;
      rememberWechatBinding(binding);
      setFlow({ importId: material.id, analysisId: null, reportId: null });
      nav("/review?next=replies");
    } catch (value) { setError(value); } finally { setBusy(""); }
  }

  return <div className="space-y-5">
    <Card title="数据库读取与增量同步" right={<Badge>DSH · 默认</Badge>}>
      <p className="text-sm text-ink-2">直接读取已登录账号的本机聊天数据库，保留消息类型和逐条时间，不需要滚动微信窗口。</p>
      <p className="mt-3 text-xs text-ink-3">{config?.note}</p>
      {config && !config.enabled && (
        <div className="mt-4">
          <Alert>数据库读取当前是关闭的。总开关在「设置 → 微信与桌面」里（默认关闭）。<Link className="underline" to="/settings">去设置开启</Link></Alert>
        </div>
      )}
      {config && !config.available && <Alert tone="warn">此功能需要 Windows 本机微信。仍可使用导入对话功能。</Alert>}
    </Card>
    {config?.enabled && config.available && <Card title="指定账号与好友" hint="只读取这里指定的私聊；同步不会触发发送。">
      <fieldset disabled={!!busy} className="space-y-4">
        <RelationshipPicker label="保存到哪个关系对象" />
        <Field label="本机微信账号" hint="账号后显示微信昵称；当前登录的账号会单独标记。"><select className="input" value={account} onChange={event => { setAccount(event.target.value); setContact(""); setContactId(""); setFriendQuery(""); setAutoSync(false); }}><option value="">请选择账号</option>{accounts.map(item => <option key={item.id} value={item.id}>{item.display_label || item.label}</option>)}</select></Field>
        {!accounts.length && <Alert tone="warn">未发现本机微信数据库。请确认微信已登录，再刷新账号列表。需要时可在页面上方手动切换到备用画面读取。</Alert>}
        {account && <div className="space-y-3">
          <div className="flex items-center justify-between gap-3"><p className="text-sm text-ink-2">{contactsLoading ? "正在加载此账号的好友列表…" : directory?.account === account ? `已加载 ${friends.length} 位好友` : "此账号的好友列表"}</p><Button size="sm" disabled={contactsLoading} onClick={() => setContactsVersion(value => value + 1)}>刷新好友</Button></div>
          {contactsLoading && <Spinner label="首次读取通讯录可能需要稍等…" />}
          <ErrorBox error={contactsError} />
          <Field label="搜索好友" hint="可按备注、昵称、微信号或 wxid 搜索。"><input className="input" value={friendQuery} disabled={contactsLoading || !directory} autoComplete="off" placeholder="输入备注、昵称或微信号" onChange={event => setFriendQuery(event.target.value)} /></Field>
          <Field label="选择好友"><select className="input" size={7} value={filteredFriends.some(friend => friend.id === contactId) ? contactId : ""} disabled={contactsLoading || !directory} onChange={event => { const friend = friends.find(item => item.id === event.target.value); setContactId(friend?.id || ""); setContact(friend?.display_name || ""); }}>
            <option value="" disabled>{filteredFriends.length ? "请选择要读取的好友" : contactsLoading ? "正在加载…" : "没有匹配的好友"}</option>
            {filteredFriends.map(friend => <option key={friend.id} value={friend.id}>{friend.display_name}{friend.nickname && friend.nickname !== friend.display_name ? ` · ${friend.nickname}` : ""} · {friend.alias || friend.id}</option>)}
          </select></Field>
          {!!directory && <p className="text-xs text-ink-3">匹配 {filteredFriends.length} / {friends.length} 位好友{selectedFriend ? `；已选：${selectedFriend.display_name}（${selectedFriend.alias || selectedFriend.id}）` : ""}</p>}
        </div>}
        <Field label="每次读取的消息条数" hint="10–5000 条，包括文字、图片、语音和卡片。"><input className="input" type="number" min={10} max={5000} value={limit} onChange={event => setLimit(Number(event.target.value))} /></Field>
        <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={transcribeVoice} onChange={event => setTranscribeVoice(event.target.checked)} />读取时将语音转成文字</label>
        <p className="text-xs text-ink-3">语音在本机识别，转写文字随记录一起导出。未下载的语音请先在电脑版微信播放；关闭此项会跳过新转写，已有结果保留。</p>
        <div className="flex flex-wrap gap-3"><Button variant="primary" disabled={contactsLoading || !selectedFriend || !account || !contact.trim() || !Number.isInteger(limit) || limit < 10 || limit > 5000} onClick={() => void read(false)}>读取最近消息</Button><Button onClick={() => void loadAccounts(true).catch(setError)}>刷新账号列表</Button></div>
      </fieldset>
    </Card>}
    {busy && <div className="flex items-center gap-3"><Spinner label={busy} />{job.current && <Button onClick={() => { setAutoSync(false); void api.post(`/wechat/dsh/jobs/${job.current}/cancel`, {}).catch(setError); }}>停止读取</Button>}</div>}
    <ErrorBox error={error} />
    {snapshot && <Card title={`已读取：${snapshot.contact}`} right={<Badge tone="good">{snapshot.messages.length} 条消息</Badge>}>
      <div className="flex flex-wrap items-center gap-3"><Button disabled={!!busy} onClick={() => void read(true)}>同步新消息</Button><label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={autoSync} disabled={!!busy} onChange={event => setAutoSync(event.target.checked)} />页面打开时自动同步</label>{snapshot.added !== undefined && <span className="text-xs text-ink-3">本次新增 {snapshot.added} 条</span>}</div>
      <p className="my-3 text-xs text-ink-3">同步进度与消息一起加密保存。重新连接可继续同步；页面关闭后停止轮询。最近 5000 条保留在同步窗口中。</p>
      {!!snapshot.voice_transcription?.total && <p className="mb-3 text-sm text-ink-2">语音 {snapshot.voice_transcription.total} 条：已转写 {snapshot.voice_transcription.completed} 条，未能转写 {snapshot.voice_transcription.unavailable} 条，待转写 {snapshot.voice_transcription.pending} 条。导入并校对后，可在聊天归档中导出。</p>}
      <div className="wechat-ocr-list">{snapshot.messages.slice(page * 50, (page + 1) * 50).map(message => <div key={message.id} className={`wechat-ocr-message ${message.sender === 'me' ? 'is-me' : ''}`}><Badge>{message.sender === "me" ? "我" : message.sender === "other" ? "对方" : message.sender === "system" ? "系统" : "角色待核对"}</Badge><p>{message.text || (typeof message.meta?.placeholder === "string" ? message.meta.placeholder : `[${message.type}]`)}</p>{message.quoted_text && <p className="text-xs text-ink-3">引用：{message.quoted_text}</p>}<p className="text-xs text-ink-3">{message.ts?.replace("T", " ") || "时间待核对"}</p></div>)}</div>
      {snapshot.messages.length > 50 && <div className="mt-3 flex items-center justify-between"><Button size="sm" disabled={!page} onClick={() => setPage(value => value - 1)}>上一页</Button><span>{page + 1} / {Math.ceil(snapshot.messages.length / 50)}</span><Button size="sm" disabled={(page + 1) * 50 >= snapshot.messages.length} onClick={() => setPage(value => value + 1)}>下一页</Button></div>}
      {snapshot.warnings.map(warning => <p className="mt-2 text-xs text-ink-3" key={warning.code}>{warning.msg}</p>)}
      <div className="mt-5 flex flex-wrap gap-3"><Button disabled={!!busy || !snapshot.messages.length} onClick={() => void useMaterial(false)}>导入并校对分析</Button><Button disabled={!!busy || !snapshot.messages.length} onClick={() => void useMaterial(true)}>校对后生成草稿</Button><Button variant="primary" disabled={!!busy || !snapshot.messages.length} onClick={() => void bindForReply()}>准备文字回复（无需截图）</Button><Button disabled={!!busy || !snapshot.messages.length} onClick={() => void bindForReply(true)}>准备图片/表情回复（需画面）</Button></div>
      <p className="mt-3 text-xs text-ink-3">文字回复从数据库核对最新消息和发送记录，聊天区被悬浮控件遮挡也能读取。准备回复会打开指定好友；发送时仍需微信输入框和发送按钮可操作。材料校对后，具体发送内容仍需预览确认。</p>
    </Card>}
  </div>;
}
