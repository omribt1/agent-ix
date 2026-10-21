/**
 * Copyright 2026 Google LLC
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     https://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

/**
 * The smallest A2A client that works: JSON-RPC `message/send`, no streaming.
 * It sends what Gemini Enterprise sends: the action as an A2UI v0.9 DataPart plus
 * `context.prompt` as text, and it reads text + A2UI parts from the final message.
 */

const A2UI_MIME = 'application/json+a2ui';
const A2UI_EXTENSION = 'https://a2ui.org/a2a-extension/a2ui/v0.9';

export type Part =
  | { kind: 'text'; text: string }
  | { kind: 'data'; data: any; metadata?: Record<string, unknown> };

export interface Reply {
  contextId: string;
  failed: boolean;
  texts: string[];
  a2ui: any[];
}

export async function send(parts: Part[], contextId?: string): Promise<Reply> {
  const res = await fetch('/a2a', {
    method: 'POST',
    headers: { 'content-type': 'application/json', 'X-A2A-Extensions': A2UI_EXTENSION },
    body: JSON.stringify({
      jsonrpc: '2.0',
      id: crypto.randomUUID(),
      method: 'message/send',
      params: {
        message: { role: 'user', messageId: crypto.randomUUID(), contextId, parts },
      },
    }),
  });
  const body = await res.json();
  if (body.error) throw new Error(body.error.message ?? 'A2A error');
  const task = body.result;
  const replyParts: Part[] = task.status?.message?.parts ?? [];
  return {
    contextId: task.contextId,
    failed: task.status?.state === 'failed',
    texts: replyParts.flatMap(p => (p.kind === 'text' ? [p.text] : [])),
    a2ui: replyParts.flatMap(p =>
      p.kind === 'data' && p.metadata?.mimeType === A2UI_MIME ? [p.data] : [],
    ),
  };
}

export const textPart = (text: string): Part => ({ kind: 'text', text });

export const actionPart = (action: unknown): Part => ({
  kind: 'data',
  data: { version: 'v0.9', action },
  metadata: { mimeType: A2UI_MIME },
});
