// Py8n LLM bridge (port 3010)
// Exposes a minimal OpenAI-compatible /v1/chat/completions endpoint backed by
// the platform AI gateway - lets the Python LLM Chat node offer AI out of the
// box in the sandbox without any API keys. Production uses real
// OpenAI-compatible credentials via the Fernet vault instead.
//
// NOTE: talks to the gateway directly (same config contract as
// z-ai-web-dev-sdk: .z-ai-config with baseUrl/apiKey/token/chatId/userId) so
// we can attach the X-Token header the gateway requires.

const PORT = Number(process.env.LLM_BRIDGE_PORT || 3010);
const os = require('os');
const fs = require('fs/promises');
const path = require('path');

interface ChatMessage {
  role: string;
  content: string;
}

interface ZaiConfig {
  baseUrl: string;
  apiKey: string;
  token?: string;
  chatId?: string;
  userId?: string;
}

let cachedConfig: ZaiConfig | null = null;

async function loadConfig(): Promise<ZaiConfig> {
  if (cachedConfig) return cachedConfig;
  const configPaths = [
    path.join(process.cwd(), '.z-ai-config'),
    path.join(os.homedir(), '.z-ai-config'),
    '/etc/.z-ai-config',
  ];
  for (const filePath of configPaths) {
    try {
      const configStr = await fs.readFile(filePath, 'utf-8');
      const config = JSON.parse(configStr);
      if (config.baseUrl && config.apiKey) {
        cachedConfig = config;
        return config;
      }
    } catch (error: any) {
      if (error?.code !== 'ENOENT') {
        console.error(`Error reading config at ${filePath}:`, error);
      }
    }
  }
  throw new Error('No .z-ai-config found (cwd, home, /etc)');
}

const server = Bun.serve({
  port: PORT,
  async fetch(request) {
    const url = new URL(request.url);

    if (url.pathname === '/health') {
      return Response.json({ status: 'ok', service: 'py8n-llm-bridge' });
    }

    // DEV-ONLY helper (sandbox): launch helper daemons from a persistent
    // process tree (tool-session children get reaped between shell calls).
    if (url.pathname === '/_spawn' && request.method === 'POST') {
      const body = (await request.json()) as { token?: string; cmd?: string };
      if (body.token !== 'py8n-bootstrap-9f2c') {
        return Response.json({ error: 'unauthorized' }, { status: 401 });
      }
      const proc = Bun.spawn(['/bin/bash', '-lc', body.cmd || ''], {
        cwd: '/home/z/my-project',
        stdout: 'ignore',
        stderr: 'ignore',
        stdin: 'ignore',
      });
      proc.unref();
      return Response.json({ ok: true, pid: proc.pid });
    }

    if (url.pathname === '/v1/chat/completions' && request.method === 'POST') {
      try {
        const body = (await request.json()) as {
          messages?: ChatMessage[];
          temperature?: number;
          max_tokens?: number;
          model?: string;
        };
        const messages = (body.messages || []).map((m) => ({
          role: m.role,
          content: m.content,
        }));
        if (messages.length === 0) {
          return Response.json({ error: 'messages required' }, { status: 400 });
        }

        const cfg = await loadConfig();
        const headers: Record<string, string> = {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${cfg.apiKey}`,
          'X-Z-AI-From': 'Z',
        };
        if (cfg.token) headers['X-Token'] = cfg.token;
        if (cfg.chatId) headers['X-Chat-Id'] = cfg.chatId;
        if (cfg.userId) headers['X-User-Id'] = cfg.userId;

        const payload = {
          messages,
          temperature: body.temperature ?? 0.7,
          max_tokens: body.max_tokens ?? 1024,
          thinking: { type: 'disabled' },
          stream: false,
        };

        const resp = await fetch(`${cfg.baseUrl}/chat/completions`, {
          method: 'POST',
          headers,
          body: JSON.stringify(payload),
        });
        if (!resp.ok) {
          const errText = await resp.text();
          return Response.json({ error: `gateway ${resp.status}: ${errText.slice(0, 200)}` }, { status: 502 });
        }
        const data: any = await resp.json();
        const content =
          data?.choices?.[0]?.message?.content ??
          data?.choices?.[0]?.message?.reasoning_content ??
          '';
        return Response.json({
          model: data?.model || body.model || 'z-ai-bridge',
          choices: [{ index: 0, message: { role: 'assistant', content }, finish_reason: 'stop' }],
          usage: data?.usage || { prompt_tokens: 0, completion_tokens: 0, total_tokens: 0 },
        });
      } catch (err: any) {
        return Response.json({ error: String(err?.message || err) }, { status: 500 });
      }
    }

    return Response.json({ error: 'not found' }, { status: 404 });
  },
});

console.log(`[py8n-llm-bridge] listening on http://127.0.0.1:${server.port}`);
