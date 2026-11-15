// AGL 编辑器支持：语法高亮（TextMate）+ 语言服务（agl-lsp）。
//
// 这里**故意不依赖 vscode-languageclient**：那需要 npm install 才能装起来，
// 而 AGL 的定位是"别的 AI / 普通人拿到就能用"。所以 LSP 的传输层自己实现
// （Content-Length 头 + JSON-RPC），一共不到一百行，换来零安装。
//
// 高亮不需要语言服务就能工作；只有装了 Python 且能找到仓库时，语言服务才启动，
// 找不到就只给高亮，并在输出面板说明原因 —— 不弹窗、不阻塞编辑。

const { spawn } = require('child_process');
const fs = require('fs');
const path = require('path');
const vscode = require('vscode');

const LANG_ID = 'agl';
const SERVER_MODULE = path.join('compiler', 'lsp.py');

let client = null;
let statusBar = null;
let output = null;

// ════════════════════════════════════════════════════════════════════
// LSP 客户端（最小实现）
// ════════════════════════════════════════════════════════════════════
class LspClient {
  constructor(command, cwd) {
    this.nextId = 1;
    this.pending = new Map();
    this.buffer = Buffer.alloc(0);
    this.diagnostics = vscode.languages.createDiagnosticCollection('agl');
    this.disposables = [this.diagnostics];
    this.alive = false;

    output.appendLine(`[agl-lsp] 启动：${command}（工作目录 ${cwd}）`);
    this.proc = spawn(command, {
      cwd,
      shell: true,          // Windows 上 python 往往是个 .bat 或别名
      stdio: ['pipe', 'pipe', 'pipe'],
    });

    this.proc.stdout.on('data', (chunk) => this._onData(chunk));
    this.proc.stderr.on('data', (chunk) => {
      output.appendLine(`[agl-lsp] ${chunk.toString('utf8').trimEnd()}`);
    });
    this.proc.on('exit', (code) => {
      this.alive = false;
      this._setStatus('语言服务未运行', 'warning');
      output.appendLine(`[agl-lsp] 已退出（code=${code}）`);
    });
    this.proc.on('error', (err) => {
      this.alive = false;
      this._setStatus('语言服务启动失败', 'warning');
      output.appendLine(`[agl-lsp] 启动失败：${err.message}`);
    });
  }

  _setStatus(text, kind) {
    if (!statusBar) return;
    statusBar.text = `$(symbol-method) AGL：${text}`;
    statusBar.tooltip = 'AGL 语言服务状态';
    statusBar.show();
  }

  // ── 传输 ────────────────────────────────────────────────────────
  _onData(chunk) {
    this.buffer = Buffer.concat([this.buffer, chunk]);
    while (true) {
      const sep = this.buffer.indexOf('\r\n\r\n');
      if (sep < 0) return;
      const header = this.buffer.slice(0, sep).toString('ascii');
      const m = /Content-Length:\s*(\d+)/i.exec(header);
      if (!m) { this.buffer = this.buffer.slice(sep + 4); continue; }
      const len = parseInt(m[1], 10);
      const start = sep + 4;
      if (this.buffer.length < start + len) return;
      const body = this.buffer.slice(start, start + len).toString('utf8');
      this.buffer = this.buffer.slice(start + len);
      try {
        this._dispatch(JSON.parse(body));
      } catch (e) {
        output.appendLine(`[agl-lsp] 收到无法解析的消息：${e.message}`);
      }
    }
  }

  _send(msg) {
    if (!this.proc || !this.proc.stdin.writable) return;
    const data = Buffer.from(JSON.stringify(msg), 'utf8');
    this.proc.stdin.write(`Content-Length: ${data.length}\r\n\r\n`);
    this.proc.stdin.write(data);
  }

  notify(method, params) {
    this._send({ jsonrpc: '2.0', method, params });
  }

  request(method, params) {
    const id = this.nextId++;
    return new Promise((resolve) => {
      const timer = setTimeout(() => {
        if (this.pending.has(id)) {
          this.pending.delete(id);
          resolve(null);
        }
      }, 4000);
      this.pending.set(id, (result) => { clearTimeout(timer); resolve(result); });
      this._send({ jsonrpc: '2.0', id, method, params });
    });
  }

  _dispatch(msg) {
    if (msg.id !== undefined && msg.id !== null && this.pending.has(msg.id)) {
      const done = this.pending.get(msg.id);
      this.pending.delete(msg.id);
      done(msg.error ? null : msg.result);
      return;
    }
    if (msg.method === 'textDocument/publishDiagnostics') {
      this._publish(msg.params);
    }
  }

  // ── 诊断 ────────────────────────────────────────────────────────
  _publish(params) {
    if (!params || !params.uri) return;
    const uri = vscode.Uri.parse(params.uri);
    const items = (params.diagnostics || []).map((d) => {
      const range = new vscode.Range(
        d.range.start.line, d.range.start.character,
        d.range.end.line, d.range.end.character);
      const diag = new vscode.Diagnostic(range, d.message, _severity(d.severity));
      diag.code = d.code;
      diag.source = 'agl';
      return diag;
    });
    this.diagnostics.set(uri, items);
  }

  // ── 生命周期 ────────────────────────────────────────────────────
  async start() {
    const init = await this.request('initialize', {
      processId: process.pid,
      rootUri: null,
      capabilities: {
        textDocument: {
          synchronization: { didSave: true, dynamicRegistration: false },
          hover: { contentFormat: ['markdown', 'plaintext'] },
          completion: { completionItem: { snippetSupport: false } },
          documentSymbol: { hierarchicalDocumentSymbolSupport: false },
          definition: {},
        },
      },
    });
    if (!init) {
      this._setStatus('语言服务握手失败', 'warning');
      return false;
    }
    this.alive = true;
    this.notify('initialized', {});
    this._setStatus('就绪', 'ok');
    output.appendLine(`[agl-lsp] 握手成功：${init.serverInfo && init.serverInfo.version}`);
    return true;
  }

  open(doc) {
    if (!this.alive) return;
    this.notify('textDocument/didOpen', {
      textDocument: { uri: doc.uri.toString(), languageId: LANG_ID, version: doc.version, text: doc.getText() },
    });
  }

  change(doc) {
    if (!this.alive) return;
    this.notify('textDocument/didChange', {
      textDocument: { uri: doc.uri.toString(), version: doc.version },
      contentChanges: [{ text: doc.getText() }],
    });
  }

  close(doc) {
    if (!this.alive) return;
    this.notify('textDocument/didClose', { textDocument: { uri: doc.uri.toString() } });
    this.diagnostics.delete(doc.uri);
  }

  stop() {
    if (!this.proc) return;
    try {
      this.notify('shutdown', null);
      this.notify('exit', null);
      this.proc.stdin.end();
      setTimeout(() => { try { this.proc.kill(); } catch (e) { /* 已退出 */ } }, 500);
    } catch (e) { /* 进程可能已不在 */ }
  }
}

function _severity(n) {
  switch (n) {
    case 1: return vscode.DiagnosticSeverity.Error;
    case 2: return vscode.DiagnosticSeverity.Warning;
    case 3: return vscode.DiagnosticSeverity.Information;
    default: return vscode.DiagnosticSeverity.Hint;
  }
}

// ════════════════════════════════════════════════════════════════════
// 找得到编译器才启语言服务
// ════════════════════════════════════════════════════════════════════
function findRepoRoot(samplePath) {
  const starts = [];
  for (const folder of vscode.workspace.workspaceFolders || []) {
    starts.push(folder.uri.fsPath);
  }
  if (samplePath) starts.push(path.dirname(samplePath));
  starts.push(__dirname);

  for (const start of starts) {
    let dir = start;
    for (let i = 0; i < 12; i++) {
      try {
        if (fs.existsSync(path.join(dir, SERVER_MODULE))) return dir;
      } catch (e) { /* 无权限就继续往上 */ }
      const parent = path.dirname(dir);
      if (parent === dir) break;
      dir = parent;
    }
  }
  return null;
}

function pythonCommand() {
  const cfg = vscode.workspace.getConfiguration('agl');
  const py = (cfg.get('pythonPath') || 'python').strip() || 'python';
  const mod = cfg.get('serverModule') || 'compiler.lsp';
  return `${py} -m ${mod}`;
}

// ════════════════════════════════════════════════════════════════════
// 激活
// ════════════════════════════════════════════════════════════════════
function activate(context) {
  output = vscode.window.createOutputChannel('AGL');
  statusBar = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right, 90);
  context.subscriptions.push(output, statusBar);

  const sample = (vscode.window.activeTextEditor &&
    vscode.window.activeTextEditor.document.languageId === LANG_ID)
    ? vscode.window.activeTextEditor.document.uri.fsPath : null;
  // 优先在 AGL 仓库里找；找不到也不放弃——`pip install -e` 之后
  // `python -m compiler.lsp` 在任何目录都能起来，用户的项目不必在仓库内。
  const root = findRepoRoot(sample)
    || (vscode.workspace.workspaceFolders && vscode.workspace.workspaceFolders[0]
        ? vscode.workspace.workspaceFolders[0].uri.fsPath : null)
    || path.dirname(sample || __dirname);

  client = new LspClient(pythonCommand(), root);
  _registerWithServer(context, client);

  client.start().catch((e) => output.appendLine(`[agl-lsp] 启动异常：${e.message}`));

  const sync = (doc) => client && client.alive && doc.languageId === LANG_ID;
  context.subscriptions.push(
    vscode.workspace.onDidOpenTextDocument((d) => { if (sync(d)) client.open(d); }),
    vscode.workspace.onDidChangeTextDocument((e) => { if (sync(e.document)) client.change(e.document); }),
    vscode.workspace.onDidCloseTextDocument((d) => { if (d.languageId === LANG_ID && client) client.close(d); }),
  );
  // 已经开着的文件补一次 didOpen，否则它们要等下一次修改才有诊断
  for (const doc of vscode.workspace.textDocuments) {
    if (doc.languageId === LANG_ID) setTimeout(() => sync(doc) && client.open(doc), 300);
  }
}

function _registerHighlightOnly(context) {
  // 高亮由 TextMate 语法提供，这里不需要注册任何 provider。
  context.subscriptions.push({ dispose: () => {} });
}

function _registerWithServer(context, c) {
  const loc = (uri, range) => new vscode.Location(
    vscode.Uri.parse(uri),
    new vscode.Range(range.start.line, range.start.character,
      range.end.line, range.end.character));

  context.subscriptions.push(
    vscode.languages.registerHoverProvider(LANG_ID, {
      async provideHover(doc, pos) {
        const r = await c.request('textDocument/hover', {
          textDocument: { uri: doc.uri.toString() },
          position: { line: pos.line, character: pos.character },
        });
        if (!r || !r.contents) return null;
        const text = typeof r.contents === 'string' ? r.contents : r.contents.value;
        const md = new vscode.MarkdownString(text);
        if (r.range) {
          return new vscode.Hover(md, new vscode.Range(
            r.range.start.line, r.range.start.character,
            r.range.end.line, r.range.end.character));
        }
        return new vscode.Hover(md);
      },
    }),

    vscode.languages.registerCompletionItemProvider(LANG_ID, {
      async provideCompletionItems(doc, pos) {
        const r = await c.request('textDocument/completion', {
          textDocument: { uri: doc.uri.toString() },
          position: { line: pos.line, character: pos.character },
        });
        if (!r || !r.items) return [];
        return r.items.map((it) => {
          const item = new vscode.CompletionItem(it.label, _completionKind(it.kind));
          item.detail = it.detail;
          item.insertText = it.insertText || it.label;
          if (it.documentation) item.documentation = new vscode.MarkdownString(it.documentation);
          if (it.sortText) item.sortText = it.sortText;
          return item;
        });
      },
    }, '.'),

    vscode.languages.registerDocumentSymbolProvider(LANG_ID, {
      async provideDocumentSymbols(doc) {
        const r = await c.request('textDocument/documentSymbol', {
          textDocument: { uri: doc.uri.toString() },
        });
        if (!r) return [];
        return r.map((s) => new vscode.SymbolInformation(
          s.name, vscode.SymbolKind.Function, '',
          new vscode.Location(doc.uri, new vscode.Range(
            s.range.start.line, s.range.start.character,
            s.range.end.line, s.range.end.character))));
      },
    }),

    vscode.languages.registerDefinitionProvider(LANG_ID, {
      async provideDefinition(doc, pos) {
        const r = await c.request('textDocument/definition', {
          textDocument: { uri: doc.uri.toString() },
          position: { line: pos.line, character: pos.character },
        });
        if (!r || !r.length) return null;
        return r.map((l) => loc(l.uri, l.range));
      },
    }),
  );
}

function _completionKind(n) {
  switch (n) {
    case 3: return vscode.CompletionItemKind.Function;
    case 10: return vscode.CompletionItemKind.Property;
    case 14: return vscode.CompletionItemKind.Keyword;
    default: return vscode.CompletionItemKind.Text;
  }
}

function deactivate() {
  if (client) client.stop();
  client = null;
}

module.exports = { activate, deactivate };
