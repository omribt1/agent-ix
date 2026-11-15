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

import { LitElement, html, nothing } from 'lit';
import { repeat } from 'lit/directives/repeat.js';
import { MessageProcessor, type ActionPayload } from '@a2ui/web_core/v0_9';
import '@a2ui/lit/v0_9'; // defines <a2ui-surface>
import { actionPart, send, textPart, type Part } from './a2a.js';
import { geCatalog } from './material-catalog.js';

interface Turn {
  id: number;
  role: 'user' | 'agent';
  texts: string[];
  surfaceIds: string[];
  error?: boolean;
}

class AgentIxApp extends LitElement {
  static properties = {
    turns: { state: true },
    busy: { state: true },
  };

  turns: Turn[] = [];
  busy = false;
  private contextId?: string;
  private nextId = 0;
  private processor = new MessageProcessor([geCatalog], (a: ActionPayload) => this.onAction(a));

  protected createRenderRoot() {
    return this;
  }

  private push(turn: Omit<Turn, 'id'>) {
    this.turns = [...this.turns, { ...turn, id: this.nextId++ }];
    this.updateComplete.then(() => window.scrollTo({ top: document.body.scrollHeight, behavior: 'smooth' }));
  }

  // Same contract as GE: the prompt is what the user "said", the action is what the agent acts on.
  private onAction(action: ActionPayload) {
    const prompt = typeof action.context?.prompt === 'string' ? action.context.prompt : action.name;
    this.exchange(prompt, [textPart(prompt), actionPart(action)]);
  }

  private async exchange(shown: string, parts: Part[]) {
    if (this.busy) return;
    this.push({ role: 'user', texts: [shown], surfaceIds: [] });
    this.busy = true;
    try {
      const reply = await send(parts, this.contextId);
      this.contextId = reply.contextId;
      this.processor.processMessages(reply.a2ui);
      const surfaceIds = reply.a2ui.flatMap(m => (m.createSurface ? [m.createSurface.surfaceId] : []));
      this.push({ role: 'agent', texts: reply.texts, surfaceIds, error: reply.failed });
    } catch (e) {
      this.push({ role: 'agent', texts: [String(e)], surfaceIds: [], error: true });
    } finally {
      this.busy = false;
    }
  }

  private onSubmit(e: SubmitEvent) {
    e.preventDefault();
    const input = (e.target as HTMLFormElement).elements.namedItem('q') as HTMLInputElement;
    const text = input.value.trim();
    if (!text) return;
    input.value = '';
    this.exchange(text, [textPart(text)]);
  }

  render() {
    return html`
      <header><strong>Agent-I:X</strong> <span>local studio · A2UI v0.9 · GE catalog</span></header>
      <main>
        ${this.turns.length ? nothing : html`<p class="hint">Try: “A hero shot of a ceramic coffee mug for our spring campaign”.</p>`}
        ${repeat(this.turns, t => t.id, t => html`
          <div class="turn ${t.role} ${t.error ? 'error' : ''}">
            ${t.texts.map(x => html`<p>${x}</p>`)}
            ${t.surfaceIds.map(id => html`<a2ui-surface .surface=${this.processor.getSurface(id)}></a2ui-surface>`)}
          </div>`)}
        ${this.busy ? html`<div class="turn agent busy">Working on it…</div>` : nothing}
      </main>
      <form @submit=${this.onSubmit}>
        <input name="q" autocomplete="off" placeholder="Describe an image, or ask for a change…" ?disabled=${this.busy} />
        <button ?disabled=${this.busy}>Send</button>
      </form>`;
  }
}

customElements.define('agent-ix-app', AgentIxApp);
