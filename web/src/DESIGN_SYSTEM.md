# GLM Desk Design System

Source: `ui-ux-pro-max` data-dense operations dashboard recommendation.

## Product Pattern

GLM Desk is an operations dashboard, not a marketing page. The interface prioritizes fast scanning, dense account rows, clear action states, and QR visibility.

## Tokens

- Background: layered warm/cool radial gradients over `#f8fafc`.
- Panel: `rgba(255, 255, 255, 0.88)` with `0 24px 70px rgba(15, 23, 42, 0.1)`.
- Ink: `#172033` for primary text.
- Muted: `#64748b` for secondary text.
- Accent: `#c99724`, keeping the old gold direction.
- Support blue: `#3b82f6` for operational status surfaces.
- Warm: `#f97316` for QR/payment attention.
- Radius: 14px controls, 20-28px panels.

## Layout

- `AppShell` owns the hero command bar and primary actions.
- `DashboardStats` creates three quick KPI cards: accounts, running flows, QR-ready rows.
- `AccountTable` keeps dense row operations on desktop.
- Mobile and narrow windows use a focusable horizontal table region to avoid viewport breakage.
- `ImportAccountModal` and `AccountContextModal` keep destructive/long-form content out of the main table.

## States

- Loading: table content is wrapped in `NSpin`.
- Empty: `NEmpty` is rendered when no accounts exist.
- Error/success: `StatusBanner` uses `NAlert` and receives all async errors from `useDashboard`.
- Pending actions: per-action loading keys disable visual ambiguity during import, sync, delete, run, and pause.

## Accessibility

- Primary interactive elements are real buttons, not clickable divs.
- Focus rings are visible for keyboard users.
- Table overflow container has `role="region"`, an accessible label, and `tabindex="0"`.
- QR image uses descriptive alt text and lazy loading.
- Motion is wrapped in `prefers-reduced-motion: no-preference`.