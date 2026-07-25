## Design Context

### Users
Professional video editors and content creators with varying skill levels — from experienced NLE users to creators who rely on AI assistance. They're working in a browser, expecting responsiveness and density comparable to desktop editors (DaVinci Resolve, Premiere Pro). They need to move fast, preview in real-time, and trust the tool with professional output.

### Brand Personality
**Capable, cinematic, warm.** Cassette positions itself as an AI co-editor — not a toy, not an enterprise behemoth. The cassette metaphor evokes a tactile, analog warmth that contrasts with the AI-forward technology underneath. The brand should feel like a skilled editing partner: confident, focused, and never in the way.

### Aesthetic Direction
- **Theme**: Dark-first (near-black backgrounds, cool grays). Warm coral/orange primary accent differentiates from the blue/purple palette of competing editors.
- **Visual language**: Cinematic — inspired by professional NLEs. DaVinci-style accent stripes on timeline clips, filmstrip thumbnails, desaturated clip colors by type.
- **Typography**: Inter (current) for UI density. JetBrains Mono imported but underused.
- **Token system**: Comprehensive HSL-based design tokens in `src/index.css` with spacing, radius, shadow, motion, and color scales.
- **Component library**: shadcn/ui (49 components) + Radix primitives + CVA variants.

### Design Principles
1. **Density without clutter** — Professional editors need information density. Every pixel should earn its place, but visual noise must be managed through hierarchy, not removal.
2. **Warm professionalism** — The coral accent and warm clip tones set Cassette apart from cold, clinical competitors. Lean into this warmth without becoming playful.
3. **AI as co-pilot, not spectacle** — The AI features should integrate seamlessly into the editing workflow. The agent ring effect is the right idea — ambient awareness, not flashy interruption.
4. **Cinematic craft** — The tool makes videos; it should feel like it was made by people who care about visual craft. Typography, spacing, and motion should reflect that care.
5. **Progressive disclosure** — Surface the most-used controls; reveal power features through interaction. Don't overwhelm newcomers or slow down experts.
