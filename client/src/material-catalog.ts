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
 * A small Lit implementation of the Gemini Enterprise composite catalog: only the
 * components the studio emits. The agent sends the exact same A2UI to GE and to
 * this client; the catalog id is what tells the renderer which widgets to use.
 *
 * The `REF:` descriptions are how @a2ui/web_core decides which props to bind to
 * the data model (Dynamic*), turn into closures (Action) or treat as children.
 */

import { html, nothing, type PropertyValues } from 'lit';
import { repeat } from 'lit/directives/repeat.js';
import { styleMap } from 'lit/directives/style-map.js';
import { z } from 'zod';
import {
  ActionSchema,
  BASIC_FUNCTIONS,
  Catalog,
  ChildListSchema,
  ComponentIdSchema,
  DynamicNumberSchema,
  DynamicStringSchema,
} from '@a2ui/web_core/v0_9';
import { A2uiLitElement } from '@a2ui/lit/v0_9';
import catalogCss from './catalog.css?inline';

export const GE_CATALOG_ID =
  'https://www.gstatic.com/vertexaisearch/a2ui/v0_9/gemini_enterprise_composite_catalog.json';

const str = (d = '') => DynamicStringSchema.describe(`REF:#/$defs/DynamicString|${d}`);
const num = (d = '') => DynamicNumberSchema.describe(`REF:#/$defs/DynamicNumber|${d}`);
const action = () => ActionSchema.describe('REF:#/$defs/Action');
const children = () => ChildListSchema.describe('REF:#/$defs/ChildList');
const option = z.object({ label: z.string(), value: z.string() });

// Props GE accepts on every Material component; we honour `style` and `weight`.
const common = {
  weight: z.number().optional(),
  style: z.record(z.string(), z.string()).optional(),
  ariaLabel: str().optional(),
};

const api = <S extends z.ZodRawShape>(name: string, shape: S) => ({
  name,
  // passthrough: GE props we don't render must not fail validation.
  schema: z.object({ ...common, ...shape }).passthrough(),
});

const JUSTIFY: Record<string, string> = {
  start: 'flex-start', center: 'center', end: 'flex-end',
  spaceBetween: 'space-between', spaceAround: 'space-around', spaceEvenly: 'space-evenly',
  stretch: 'stretch',
};

type Props = Record<string, any>;
type Child = string | { id: string; basePath: string };
const childKey = (c: Child) => (typeof c === 'string' ? c : `${c.basePath}/${c.id}`);

const sheet = new CSSStyleSheet();
sheet.replaceSync(catalogCss);

/**
 * Light-DOM base so one stylesheet (catalog.css) themes the whole surface.
 * <a2ui-surface> has a shadow root, so the sheet is adopted there.
 */
abstract class MaterialElement extends A2uiLitElement {
  protected createRenderRoot() {
    return this;
  }

  connectedCallback() {
    super.connectedCallback();
    const root = this.getRootNode();
    if (root instanceof ShadowRoot && !root.adoptedStyleSheets.includes(sheet)) {
      root.adoptedStyleSheets = [...root.adoptedStyleSheets, sheet];
    }
  }

  protected get props(): Props | undefined {
    return this.controller?.props as Props | undefined;
  }

  willUpdate(changed: PropertyValues) {
    super.willUpdate(changed);
    const p = this.props;
    if (p?.weight !== undefined) this.style.flex = String(p.weight);
    Object.assign(this.style, p?.style ?? {});
  }

  protected renderChildren(list: Child[] | undefined) {
    return repeat(list ?? [], childKey, c => this.renderNode(c));
  }
}

// ---------------------------------------------------------------- layout

const TextApi = api('MaterialText', {
  text: str(),
  usageHint: z.string().optional(),
});
class MText extends MaterialElement {
  protected readonly api = TextApi;
  render() {
    const p = this.props;
    if (!p) return nothing;
    return html`<span class="m-text ${p.usageHint ?? 'body'}">${p.text}</span>`;
  }
}

const RowApi = api('MaterialRow', {
  children: children(),
  justify: z.string().optional(),
  align: z.string().optional(),
});
class MRow extends MaterialElement {
  protected readonly api = RowApi;
  render() {
    const p = this.props;
    if (!p) return nothing;
    this.style.justifyContent = JUSTIFY[p.justify] ?? '';
    this.style.alignItems = JUSTIFY[p.align] ?? '';
    return this.renderChildren(p.children);
  }
}

const ColumnApi = api('MaterialColumn', {
  children: children(),
  justify: z.string().optional(),
  align: z.string().optional(),
});
class MColumn extends MRow {
  protected readonly api = ColumnApi;
}

const CardApi = api('MaterialCard', {
  children: children(),
  appearance: z.enum(['outlined', 'raised']).optional(),
});
class MCard extends MaterialElement {
  protected readonly api = CardApi;
  render() {
    const p = this.props;
    if (!p) return nothing;
    this.dataset.appearance = p.appearance ?? 'raised';
    return this.renderChildren(p.children);
  }
}

// Locally there is no side panel; render the canvas like a card with a heading.
const CanvasApi = api('Canvas', {
  children: children(),
  cardTitle: z.string().optional(),
});
class MCanvas extends MaterialElement {
  protected readonly api = CanvasApi;
  render() {
    const p = this.props;
    if (!p) return nothing;
    return html`${p.cardTitle ? html`<div class="m-text caption">${p.cardTitle}</div>` : nothing}
      ${this.renderChildren(p.children)}`;
  }
}

const DividerApi = api('MaterialDivider', {});
class MDivider extends MaterialElement {
  protected readonly api = DividerApi;
  render() {
    return html`<hr />`;
  }
}

const TabsApi = api('MaterialTabs', {
  tabs: z.array(z.object({ label: z.string(), content: ComponentIdSchema })),
});
class MTabs extends MaterialElement {
  protected readonly api = TabsApi;
  static properties = { active: { state: true } };
  active = 0;
  render() {
    const p = this.props;
    if (!p) return nothing;
    const tabs = p.tabs as { label: string; content: string }[];
    return html`
      <div class="m-tab-bar" role="tablist">
        ${tabs.map(
          (t, i) => html`<button role="tab" aria-selected=${i === this.active}
            @click=${() => (this.active = i)}>${t.label}</button>`,
        )}
      </div>
      ${tabs[this.active] ? this.renderNode(tabs[this.active].content) : nothing}`;
  }
}

// ----------------------------------------------------------------- media

// The agent sends the basic Image: GE's MaterialImage blanks signed GCS URLs.
const ImageApi = api('Image', {
  url: str(),
  description: str().optional(),
  fit: z.string().optional(),
  variant: z.string().optional(),
});
/**
 * The agent sends the current image's url as a formatString-built SVG that reads the
 * slider values (app/studio/live_preview.py), so live preview needs nothing here: the
 * framework re-resolves the url on every data change, as Gemini Enterprise does.
 */
class MImage extends MaterialElement {
  protected readonly api = ImageApi;
  render() {
    const p = this.props;
    if (!p) return nothing;
    return html`<img src=${p.url} alt=${p.description ?? ''} class="rounded"
      style=${styleMap({ objectFit: p.fit ?? 'contain', width: '100%' })} />`;
  }
}

const VideoApi = api('Video', { url: str() });
class MVideo extends MaterialElement {
  protected readonly api = VideoApi;
  render() {
    const p = this.props;
    if (!p) return nothing;
    return html`<video src=${p.url} controls autoplay loop muted playsinline></video>`;
  }
}

// -------------------------------------------------------------- controls

const ButtonApi = api('MaterialButton', {
  label: str(),
  variant: z.string().optional(),
  leadingIcon: z.string().optional(),
  action: action(),
});
class MButton extends MaterialElement {
  protected readonly api = ButtonApi;
  render() {
    const p = this.props;
    if (!p) return nothing;
    return html`<button class="m-button ${p.variant ?? 'basic'}" @click=${() => p.action?.()}>
      ${p.leadingIcon ? html`<span class="material-symbols-outlined">${p.leadingIcon}</span>` : nothing}
      ${p.label}
    </button>`;
  }
}

// GE fires the chip action on selection, after writing the value to the model.
const ChipsApi = api('MaterialChips', {
  value: str(),
  options: z.array(option),
  action: action().optional(),
});
class MChips extends MaterialElement {
  protected readonly api = ChipsApi;
  render() {
    const p = this.props;
    if (!p) return nothing;
    return html`<div class="m-chips" role="listbox">
      ${(p.options as z.infer<typeof option>[]).map(
        o => html`<button role="option" aria-selected=${p.value === o.value}
          @click=${() => {
            p.setValue?.(o.value);
            p.action?.();
          }}>${o.label}</button>`,
      )}
    </div>`;
  }
}

const ToggleApi = api('MaterialButtonToggle', {
  value: str(),
  options: z.array(option),
});
class MToggle extends MaterialElement {
  protected readonly api = ToggleApi;
  render() {
    const p = this.props;
    if (!p) return nothing;
    return html`<div class="m-toggle">
      ${(p.options as z.infer<typeof option>[]).map(
        o => html`<button aria-pressed=${p.value === o.value}
          @click=${() => p.setValue?.(o.value)}>${o.label}</button>`,
      )}
    </div>`;
  }
}

const SliderApi = api('MaterialSlider', {
  value: num(),
  min: z.number().optional(),
  max: z.number(),
  step: z.number().optional(),
});
class MSlider extends MaterialElement {
  protected readonly api = SliderApi;
  render() {
    const p = this.props;
    if (!p) return nothing;
    return html`<input type="range" min=${p.min ?? 0} max=${p.max} step=${p.step ?? 1}
        aria-label=${p.ariaLabel ?? ''} .value=${String(p.value ?? 0)}
        @input=${(e: Event) => p.setValue?.(Number((e.target as HTMLInputElement).value))} />
      <output>${p.value ?? 0}</output>`;
  }
}

const InputApi = api('MaterialInput', {
  value: str(),
  label: str().optional(),
  placeholder: str().optional(),
});
class MInput extends MaterialElement {
  protected readonly api = InputApi;
  render() {
    const p = this.props;
    if (!p) return nothing;
    return html`<label class="m-input">
      <span>${p.label ?? ''}</span>
      <input type="text" placeholder=${p.placeholder ?? ''} .value=${p.value ?? ''}
        @input=${(e: Event) => p.setValue?.((e.target as HTMLInputElement).value)} />
    </label>`;
  }
}

// --------------------------------------------------------------- catalog

const entries: [ReturnType<typeof api>, typeof MaterialElement, string][] = [
  [TextApi, MText, 'ge-text'], [RowApi, MRow, 'ge-row'], [ColumnApi, MColumn, 'ge-column'],
  [CardApi, MCard, 'ge-card'], [CanvasApi, MCanvas, 'ge-canvas'], [DividerApi, MDivider, 'ge-divider'],
  [TabsApi, MTabs, 'ge-tabs'], [ImageApi, MImage, 'ge-image'], [VideoApi, MVideo, 'ge-video'],
  [ButtonApi, MButton, 'ge-button'], [ChipsApi, MChips, 'ge-chips'],
  [ToggleApi, MToggle, 'ge-button-toggle'], [SliderApi, MSlider, 'ge-slider'],
  [InputApi, MInput, 'ge-input'],
];

export const geCatalog = new Catalog(
  GE_CATALOG_ID,
  '0.9',
  entries.map(([a, element, tagName]) => ({ ...a, tagName, element })),
  // The GE catalog includes the basic functions (openUrl, formatString, ...).
  BASIC_FUNCTIONS,
);
