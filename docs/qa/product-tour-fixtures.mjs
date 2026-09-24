// Fictitious, in-memory replies for product-tour.mjs. No provider or user data.
export const models = [
  {
    spec: "demo-compatible:aurora-small",
    label: "Aurora Small · demo",
    enabled: true,
    vision: 1,
    cost_in: 0.4,
    cost_out: 1.6,
    api_key_set: false,
    api_key_env: "DEMO_OPENAI_API_KEY",
  },
  {
    spec: "demo-native:orion-review",
    label: "Orion Review · demo",
    enabled: true,
    vision: 1,
    cost_in: 3,
    cost_out: 15,
    api_key_set: true,
    api_key_hint: "DEMO_••••",
  },
  {
    spec: "demo-local:inventory-coder",
    label: "Inventory Coder · demo",
    enabled: true,
    vision: 0,
    cost_in: 0.8,
    cost_out: 2.4,
    api_key_set: false,
    api_key_env: "",
  },
  {
    spec: "demo-local:vision-unmeasured",
    label: "Vision Unmeasured · demo",
    enabled: true,
    vision: null,
    cost_in: 0.2,
    cost_out: 0.7,
    api_key_set: false,
    api_key_env: "",
  },
];

export const config = {
  config: {
    RELAY_HOST: "127.0.0.1",
    FOURBIS_REPOS_ROOT: "C:/work/demo-repos",
    GITHUB_BOARD_OWNER: "",
    GITHUB_BOARD_NUMBER: "",
    DISCORD_GUILD_ID: "",
    MODEL_PRICES: JSON.stringify({
      "demo-*": { in: 0.25, out: 0.75 },
    }, null, 2),
    FOURBIS_MODEL: "demo-compatible:aurora-small",
    FOURBIS_PLANNER_MODEL: "demo-compatible:aurora-small",
    FOURBIS_VERIFIER_MODEL: "demo-native:orion-review",
    FOURBIS_DOCUMENTER_MODEL: "demo-compatible:aurora-small",
    FOURBIS_COMPACTOR_MODEL: "",
  },
  effective: {
    bind_host: "127.0.0.1",
    localhost_guard_active: true,
    repos_root: "C:/work/demo-repos",
    version: "demo-fixture",
  },
  editable_keys: [],
  restart_required_keys: ["RELAY_HOST"],
  model_roles: {
    keys: {
      executor: "FOURBIS_MODEL",
      planner: "FOURBIS_PLANNER_MODEL",
      verifier: "FOURBIS_VERIFIER_MODEL",
      documenter: "FOURBIS_DOCUMENTER_MODEL",
      compactor: "FOURBIS_COMPACTOR_MODEL",
    },
    effective: {
      executor: "demo-compatible:aurora-small",
      planner: "demo-compatible:aurora-small",
      verifier: "demo-native:orion-review",
      documenter: "demo-compatible:aurora-small",
      compactor: "demo-compatible:aurora-small",
    },
  },
};

export const timeouts = {
  expert_timeout_s: 600,
  expert_timeout_default_s: 600,
  tool_timeout_s: 60,
  tool_timeout_default_s: 60,
};

export const projects = [
  {
    slug: "aurora-dashboard",
    name: "Aurora Dashboard",
    enabled: true,
    include_in_index: true,
    night_mode_enabled: false,
    indexed: true,
    has_git: true,
    git_remote_url: "",
    discord_channel_id: "",
    github_project: null,
  },
  {
    slug: "inventory-api",
    name: "Inventory API",
    enabled: true,
    include_in_index: true,
    night_mode_enabled: false,
    indexed: false,
    has_git: true,
    git_remote_url: "",
    discord_channel_id: "",
    github_project: null,
  },
];

export const browse = {
  root: "C:/work/demo-repos",
  depth: 2,
  count: 2,
  entries: [
    {
      path: "C:/work/demo-repos/aurora-dashboard",
      depth: 1,
      is_repo: true,
      has_expert: true,
      expert_slug: "aurora-dashboard",
      expert_enabled: true,
      cbm_indexed: true,
      cbm_nodes: 248,
      cbm_edges: 391,
    },
    {
      path: "C:/work/demo-repos/inventory-api",
      depth: 1,
      is_repo: true,
      has_expert: true,
      expert_slug: "inventory-api",
      expert_enabled: true,
      cbm_indexed: false,
      cbm_nodes: 0,
      cbm_edges: 0,
    },
  ],
};

export const indexedFiles = {
  files: [
    { path: "src/api/projects.ts", name: "projects.ts", in_degree: 4, out_degree: 7 },
    { path: "src/components/ProjectList.tsx", name: "ProjectList.tsx", in_degree: 2, out_degree: 5 },
    { path: "tests/projects.test.ts", name: "projects.test.ts", in_degree: 1, out_degree: 3 },
  ],
};
