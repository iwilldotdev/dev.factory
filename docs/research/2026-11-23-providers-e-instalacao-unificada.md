# Estudo: factory agnóstica a provider, papel do dev.mcp e instalação

Data: 2026-11-23 · Revisão 2. Substitui a versão anterior, que punha o dev.mcp no centro. · Status: análise; nada da factory foi implementado · Escopo: `dev.factory` 0.1.0, `dev.mcp` 1.2.0

## 0. Objetivo

**Desacoplar a factory do dev.mcp por completo, mantendo só os fundamentos da factory:** runs com fases e gates, evidence-or-zero (collectors gravam evidência, gates decidem), contratos de dados, intenções de escrita verificadas por readback, guards e revisão independente. Nenhum nome de servidor, de tool ou de variável do dev.mcp fica no código, nos agents, na skill, nos hooks ou no instalador da factory. O dev.mcp passa a ser só mais uma fonte possível de dados, ao lado dos MCPs oficiais e das CLIs.

### Critério de pronto do desacoplamento

Medido no `main` de hoje com `grep -rE "dev_mcp|dev\.mcp|DEV_MCP_"` e com a busca por nomes de tool (`gitlab_*`, `jira_*`, `figma_*`):

| Onde | Hoje | Pronto quando |
|---|---|---|
| CLI (`factory/fetches.py`, `core.py`, `cli.py`, `commands/{gate,review,feedback}.py`) | Monta chamadas exatas (`jira_get_issue`, `gitlab_get_merge_request`, `gitlab_get_pipeline(_jobs)`, `figma_get_design_context`, `figma_fetch_screenshot`…) e cita o dev.mcp | Os hints dizem **o que** falta ("ticket DEV-1 em `R/raw/ticket-DEV-1.json`, qualquer fonte") e, no máximo, exemplos por provider vindos de uma tabela de dados, não de código |
| Agents (`claude/agents/factory-*.md`) | `tools: … mcp__dev_mcp__*` e `disallowedTools` com as tools de escrita do dev.mcp pelo nome | Sem nome de servidor. Leitura liberada; escrita controlada pelo guard por verbo e CLI (seção 6) |
| Skill e references (`SKILL.md`, `phases.md`, `sessions.md`, `workers.md`, `figma-pack.md`, `review.md`, `ship.md`) | Instruções "use `jira_*`/`gitlab_*`/`figma_*` com `save_to`" | Instruções por contrato e intenção ("salve o bruto do ticket em `R/raw/`, rode `factory ingest ticket`") |
| Hooks (`worker-guard.js`, `protect.js` e os testes em `claude/hooks/test/`) | Citam o dev.mcp e as suas tools | Classificação genérica de escrita |
| Instalador (`install.py`: 19 menções; `scripts/install-profiles.ps1`: 11) | `merge_mcp_env()` completa `DEV_MCP_SAVE_ROOTS`/`ALLOW_DOT`/`SPILL_KB` numa entrada `dev.mcp`; `-BuildDevMcp` builda o repo irmão | Nenhuma referência. A factory não edita config de MCP de ninguém |
| `CLAUDE.snippet.md`, `README.md`, `DESIGN.md`, `AGENTS.md` | dev.mcp como pré-requisito | dev.mcp citado no máximo como exemplo de fonte |
| Testes (`tests/test_fetch_hints.py`, `tests/test_install.py`) | Afirmam nomes de tool do dev.mcp | Afirmam contratos e hints neutros |

Teste de regressão permanente: um teste em `tests/` que falha se o grep acima encontrar ocorrência fora de `docs/`, `CHANGELOG.md` e fixtures de normalizador (onde o formato `dev.mcp-figma` é só um dos formatos aceitos).

## 1. Mudança de direção

O dev.mcp nasceu para fugir da cota baixa do MCP oficial do Figma. Depois ele acabou concentrando Jira, Confluence e GitLab, e o harness e a factory foram construídos em volta dele. A versão 1 deste estudo propunha estender o dev.mcp a GitHub, Linear, Bitbucket e Azure DevOps. Esta revisão inverte a direção:

- **A factory deixa de depender de servidores e passa a depender de contratos de dados.** Agentes de código lidam bem com vários MCPs, plugins e CLIs ao mesmo tempo. Quem busca os dados é o agente, com o que o host tiver (MCP oficial, CLI ou dev.mcp). A factory normaliza o payload bruto, valida e decide com base em evidência. Ela já é quase assim, porque a CLI nunca chama a rede e só lê payloads salvos.
- **Escritas viram intenções verificadas.** A factory planeja ("abrir PR em draft", "comentar no ticket", "transicionar para Code Review"). A sessão principal executa com a tool disponível, e a factory confere relendo o serviço (readback). Vale a evidência, não a tool usada.
- **O dev.mcp volta a ser uma ferramenta do dia a dia,** para o trabalho na ConstruCode (GitLab, Jira, Confluence, Figma). Ele fica no mesmo nível dos MCPs oficiais em nomes e cobertura, e é **opcional** para a factory. Não há roadmap de vendors novos nele.

### Por que não estender o dev.mcp

| Benefício do dev.mcp | Exclusivo dele? |
|---|---|
| REST do Figma sem a cota diária do MCP oficial | **Sim.** É o diferencial real |
| `save_to`/spill (payload grande vai direto para disco) | Parcial: `gh api … > f.json`, `glab api` e redirecionamento de CLI fazem o mesmo |
| `jira_normalize_issue` (AC, links, unknowns) | Não: é parsing, cabe na factory |
| Listas fixas de tools para os guards | Parcial: dá para classificar escritas sem depender de um servidor (seção 6) |

Custos de estender: reimplementar APIs que já têm servidor oficial e acompanhar cada renomeação; atrito para quem usa open source, que teria de entregar tokens a um servidor de terceiro em vez de usar o conector que já tem; ficar atrás do oficial em funcionalidades e auth (OAuth, SSO, Entra).

## 2. Arquitetura agnóstica

```
ticket/PR/pipeline/design no provider
        │  agente busca com o que houver: MCP oficial · CLI (gh, glab, az, acli) · dev.mcp
        ▼
R/raw/<kind>-<id>.<json|png>          ← payload bruto, gravado por tool (save_to) ou por redirecionamento de CLI
        │  factory ingest <kind> <arquivo> --format <fmt|auto>   (sem rede, sem auth)
        ▼
R/<kind>/<id>.json  (contrato vN)     ← normalizado, com origem: {tool, url, fetched_at, sha256 do bruto}
        │
        ▼
gates · collectors · ship plan (intenções) → sessão principal executa → readback → ship verify
```

Regras:

1. **O bruto é sempre preservado.** O normalizador é determinístico e testado com fixtures reais de cada formato. Uma mudança de formato do vendor quebra um teste de fixture, não um run.
2. **Origem obrigatória.** Cada contrato registra a fonte e o sha256 do bruto, e o collector recalcula o hash.
3. **Contexto barato.** A aquisição preferida grava em disco sem passar pelo modelo: `save_to` (dev.mcp) ou redirecionamento de CLI. Um MCP que só devolve no contexto também serve, mas custa tokens; o agente então grava o payload com `factory ingest --stdin`.
4. **Formato desconhecido não passa.** `ingest` falha com a lista de formatos aceitos. Nada é "inferido" para fazer um gate passar.

## 3. Pilar 1: tickets

**Objetivo:** iniciar e avançar trabalho a partir de um ticket, qualquer que seja o provider.

### Contrato `ticket.v1`

| Campo | Observação |
|---|---|
| `provider`, `key`, `url`, `title`, `type`, `status` | `key` no formato do provider (`ABC-123`, `group/proj#42`, `owner/repo#42`, `ENG-42`) |
| `description_md` | Sempre Markdown. ADF (Jira) e HTML (Azure DevOps) são convertidos no normalizador |
| `acceptance_criteria[]` | Extraídos por seção conhecida ("Critérios de aceite", "Acceptance criteria", checklist) ou por campo custom configurado |
| `links[]` | Relações, PRs/MRs, URLs de design (Figma, imagens) |
| `attachments[]` | Imagens e anexos baixados para `R/raw/`, que alimentam o pilar 2 |
| `parent`/`epic`, `assignee`, `labels` | Quando existirem |
| `unknowns[]` | O que o normalizador não conseguiu ler; vai para o gap round do intake |

O `jira_normalize_issue` atual (schemaVersion 1) é o ponto de partida do contrato; a lógica passa para a factory.

### Formatos aceitos por `ingest ticket`

| Formato | Aquisição típica |
|---|---|
| `jira-rest` (REST v3, ADF) | dev.mcp `jira_get_issue`, Rovo `getJiraIssue`, `acli` |
| `gitlab-work-item` / `gitlab-issue` | MCP oficial do GitLab `get_work_item`, `glab api` |
| `github-issue` | MCP oficial do GitHub `issue_read`, `gh api repos/{o}/{r}/issues/{n}` |
| `linear-issue` | MCP oficial do Linear, GraphQL |
| `azure-work-item` | `microsoft/azure-devops-mcp` `wit_work_item`, `az boards work-item show` |

### Intenções de escrita

| Intenção | Readback |
|---|---|
| `issue_comment{body_md}` | Comentário presente no ticket (o agente converte para ADF quando a tool exigir) |
| `issue_transition{to_status}` | `status` igual ao alvo. O agente resolve a transição (Jira) ou o estado (GitHub open/closed + label, GitLab `status_id`, Linear workflow state) |
| `issue_link{url, kind}` | Link presente |

### Referências de ticket

O workspace declara `tickets.provider`, `tickets.key_regex` e `tickets.url_patterns`. Isso substitui `DEFAULT_TICKET_RE` (`factory/commands/review.py:119`) e a regex fixa do harness. `factory run start <ref>` aceita chave ou URL.

## 4. Pilar 2: design

**Objetivo:** ler design, inclusive mockups e imagens soltas, por aproximação, qualquer que seja a ferramenta.

### Dois níveis de fidelidade, declarados no pack

| Nível | Fonte | O que o pack tem | Como os gates tratam |
|---|---|---|---|
| `exact` | Ferramenta com nós estruturados: Figma via dev.mcp (`figma_get_design_context`) ou via MCP oficial | Medidas, estilos, textos e variáveis por nó, mais PNG em escala 1 | Como hoje: style maps (`factory figma expect`) e contrato visual com tamanhos fixos e posição absoluta |
| `approx` | Imagem: anexo do ticket, screenshot, mockup exportado (Figma sem acesso, Penpot, Sketch, Zeplin, Balsamiq), PDF rasterizado | PNG normalizado, mais uma **spec estimada** extraída pelo agente (regiões, medidas aproximadas, cores amostradas, textos), cada valor marcado como `estimated` | Comparação visual com tolerância maior; style checks de valor estimado viram faixa (±N px, ΔE de cor); revisão humana obrigatória no review; nunca contam como `exact` |

### Contrato `design/manifest.json`

```json
{ "schema": "design.v1", "fidelity": "exact|approx",
  "source": {"tool": "figma|image|penpot|...", "file": "...", "version": "...", "fetched_at": "..."},
  "frames": [{ "id": "3544:5710", "name": "Lista vazia", "image": "frames/3544-5710.png",
               "spec": "frames/3544-5710.json", "estimated": false }] }
```

- O `figma-pack.md` atual (meta, `<node>.json`, `<node>.png`) passa a ser o caso `exact`/`figma` desse manifesto.
- O normalizador aceita o payload do dev.mcp e o do MCP oficial do Figma. O `get_design_context` oficial devolve código de referência (React + Tailwind) e não nós crus. A partir dele dá para extrair menos propriedades, então o pack registra quais faltam, e a cobertura `exact` pode cair para `approx` nesses campos.
- Mockup em imagem: `factory design add <run> <imagem> --name … --fidelity approx` normaliza a imagem (PNG, sRGB, escala declarada) e cria o frame. A spec estimada é escrita pelo `factory-author` com origem `estimated`.

## 5. Pilar 3: entrega (PR/MR, pipeline, draft)

**Objetivo:** abrir e atualizar Pull/Merge Requests, acompanhar pipelines e colocar ou tirar de draft, qualquer que seja o provider.

### Contratos

| Contrato | Campos |
|---|---|
| `change.v1` | `provider`, `repo`, `number`, `url`, `source_branch`, `target_branch`, `head_sha`, `state`, `draft`, `title`, `reviewers[]`, `pipeline_ref` |
| `pipeline.v1` | `provider`, `id`, `url`, `sha`, `status` (normalizado: `pending`/`running`/`success`/`failed`/`canceled`/`skipped`), `jobs[]{name, stage, status, url, log_ref}` |

### Intenções e mapeamento por provider

| Intenção | GitLab | GitHub | Azure DevOps | Bitbucket |
|---|---|---|---|---|
| `change_request{create\|update}` | `save_merge_request` / `glab mr create` | `create_/update_pull_request` / `gh pr create` | `repo_pull_request_write` | API `pullrequests` |
| `set_draft{true\|false}` | campo `draft` ou prefixo `Draft:` | `gh pr ready` / `gh pr ready --undo` (GraphQL) | `isDraft` | campo `draft` |
| `change_comment{body_md}` | `save_note` | `add_issue_comment` | thread write | comentário de PR |
| `pipeline_watch{sha}` | `get_pipeline` | `actions_list`/`gh run view` | `pipelines_run` | API pipelines |

Readback: `ship verify` compara `change.v1` com o plano (alvo, draft, título, sha) e `pipeline.v1` com o sha esperado. Hoje isso está preso ao GitLab: `DRAFT_PREFIX` e `is_draft` em `factory/commands/ship.py:29,513`, campos `iid`/`web_url` e a ação `mr` em `ship.py:451-453`. O comentário ADF de `ship.py:362` vira Markdown no plano, e o agente converte quando a tool exigir.

`factory wait` acompanha pipelines relendo `pipeline.v1`, salvo pelo agente a cada intervalo, com o mesmo contrato para qualquer CI.

## 6. Guards sem depender de um servidor

Hoje os agents listam `mcp__dev_mcp__*` e negam cada tool de escrita pelo nome. O modelo agnóstico:

- **Escrita bloqueada por padrão nas fases com gate.** O `protect.js` (matcher `mcp__.*` já existe) classifica por verbo no nome da tool: `create|update|save|add|edit|write|transition|merge|accept|delete|post|ready|approve|run`. Há também uma lista explícita para os servidores conhecidos (GitHub, GitLab, Rovo, Linear, Azure DevOps, dev.mcp).
- **CLIs de escrita:** `gh pr create|edit|merge|ready|comment`, `gh issue edit|comment|close`, `gh api` com `-X POST|PATCH|PUT|DELETE` ou `-f`, `glab mr create|update|merge|note`, `glab api -X …`, `az repos pr create|update`, `curl -X POST|PATCH|PUT|DELETE`. O `worker-guard.js:32` já bloqueia `glab` e `gh` para workers.
- **Leitura liberada,** com orientação para gravar em `R/raw/`.
- Uma tool desconhecida com verbo ambíguo é tratada como escrita (fail-closed).

**Risco atual (dev.mcp 1.3.0):** a paridade com os MCPs oficiais trouxe 19 tools de escrita novas (`gitlab_save_note`, `gitlab_save_merge_request_review`, `gitlab_save_pipeline`, `confluence_update_content`, …). Elas não estão nos `disallowedTools` de `claude/agents/factory-*.md`, que listam só os nomes antigos. Por isso o dev.mcp só as registra com `DEV_MCP_PARITY_WRITES=1` (desligado por padrão). Ligar essa flag num perfil com a factory instalada só é seguro depois de uma destas duas coisas: os agents bloquearem escrita por padrão (este guard) ou as listas passarem a incluir os nomes novos.

## 7. Configuração do workspace

```json
"providers": {
  "tickets": {"kind": "jira", "site": "x.atlassian.net", "key_regex": "[A-Z][A-Z0-9]*-\\d+"},
  "scm":     {"kind": "gitlab", "host": "https://gitlab.com"},
  "design":  {"kind": "figma", "fallback": "images"}
},
"repos": [{"alias": "api", "scm_project": "grupo/api"}]
```

`workspace import` migra `gitlab_project`, `gitlab_username` e `reviewer.account_id` para os campos neutros, mantendo os antigos como alias por uma versão.

## 8. Roadmap da factory

| Fase | Conteúdo | Saída |
|---|---|---|
| **0. Contratos** | `ticket.v1`, `change.v1`, `pipeline.v1`, `design.v1`; `factory ingest`; normalizadores `jira-rest`, `gitlab-*` e Figma do dev.mcp (o que existe hoje); origem e hash | Runs GitLab+Jira+Figma sem mudança de comportamento, lendo só contratos; teste de grep verde em `factory/` |
| **1. Intenções, guards e desacoplamento** | Ações de ship neutras (`change_request`, `set_draft`, `issue_comment`, `issue_transition`) com aliases das atuais; readback por contrato; guard por verbo e CLI; agents, skill, references, hooks, instalador e docs sem dev.mcp (seção 0) | `ship verify` passando com o dev.mcp **e** com o MCP oficial do GitLab/Rovo; teste de grep da seção 0 verde no repo inteiro |
| **2. GitHub** | Normalizadores `github-issue`, `github-pr`, `github-actions`; `set_draft` via `gh` | Run completo num repo só GitHub (issue → PR draft → Actions → ready) |
| **3. Design por aproximação** | `fidelity: approx`, `factory design add`, tolerâncias e revisão humana | Run com mockup em PNG anexado ao ticket, sem Figma |
| **4. Mais formatos** | `linear-issue`, `azure-work-item`, `azure-pr`, `bitbucket-pr`, Figma via MCP oficial | Um run por formato, com fixtures |
| Backlog | ClickUp, monday, Asana, Penpot nativo | Sob demanda |

A Fase 0 mexe em `fetches.py`, `review.py` e `ship.py`, que outra sessão está revisando agora. Ela precisa começar depois dessa revisão ou ser coordenada com ela.

## 9. dev.mcp: ferramenta diária, no mesmo nível dos oficiais

**Papel:** o servidor que o William usa na ConstruCode para GitLab, Jira, Confluence e Figma. Ele substitui os MCPs oficiais sem perder nada e mantém os extras: Figma sem cota diária, `save_to`/spill, fallback `glab`, normalização de issue. Para a factory ele é **um** dos meios de aquisição.

**Convenção de nomes:** `<prefixo>_<nome oficial em snake_case>`, com os mesmos parâmetros do oficial. O Figma já segue isso (`figma_get_design_context`, `figma_get_metadata`, `figma_get_screenshot`, `figma_get_variable_defs`). O GitLab segue na maior parte. Para o Jira e o Confluence, os nomes camelCase do Rovo viram snake_case sem a palavra redundante do vendor (`getJiraIssue` → `jira_get_issue`, `searchJiraIssuesUsingJql` → `jira_search_issues_using_jql`). Os nomes antigos continuam registrados como aliases depreciados até a factory e o harness migrarem.

O detalhamento da paridade (o que foi renomeado, o que foi adicionado, o que fica fora por exigir Duo ou não ter REST) está no CHANGELOG e no README do dev.mcp.

## 10. Instalação

Com a factory agnóstica, a pergunta "unificar factory e dev.mcp" perde força:

- **A factory se instala sozinha** e não exige nenhum MCP específico. Ela verifica (`factory doctor`) se há algum meio de aquisição por pilar: um MCP com tools de ticket, design ou SCM, ou `gh`/`glab`/`az` autenticados.
- **Auth dos providers é responsabilidade do host:** conectores oficiais, OAuth do próprio MCP, `gh auth login`, `glab auth login`. A factory não guarda tokens. Isso elimina a parte de keychain e login da versão 1.
- **O dev.mcp continua um pacote separado e opcional.** Publicá-lo no npm (`npx -y dev.mcp`) só remove o clone e o build; não é pré-requisito.
- **Monorepo: não recomendado.** Sem acoplamento de nomes de tools, não há mudança atômica a proteger.

### Cross-platform (continua válido)

O fluxo guiado de hoje é só Windows (`scripts/install-profiles.ps1`). O `install.py` já é quase portável.

| Ponto | Hoje | Proposta |
|---|---|---|
| Entrypoint | `.ps1` faz PATH, build do dev.mcp por caminho irmão, checagem de host aberto e smoke test | Levar tudo para `install.py`; `install.ps1`/`install.sh` só localizam o Python. O build do dev.mcp sai do instalador |
| PATH | Registro no Windows; no Unix só imprime `export` para bash (`install.py:102-109`) | Opt-in: rc do shell detectado (zsh, bash, fish) ou `~/.local/bin` |
| Shims | `.cmd` gerado em todo SO, sem escape de `%`/`"` (`install.py:492,541-544`); o shim sh quebra com apóstrofo no home (`491,497`) | `.cmd` só no Windows, com escape; interpretador `py -3` → `python3` → `python` |
| Caminho de host | `~/.claude.json` fixo (`install.py:348-350`) | Um módulo com caminhos por host e por SO |
| Comparação de caminho | Ignora caixa só em `nt` (`install.py:589-590`); o APFS do macOS também ignora caixa | Comparar por `samefile` quando o caminho existe |
| dev.mcp | `spawn('glab')` sem shell não acha `glab.cmd` (`src/clients/glab.ts`) | Resolver pelo PATHEXT no Windows |
| CI | Nenhum em dev.factory e dev.mcp; dev.harness só Ubuntu | Matriz windows, macos, ubuntu: testes do instalador em `--dry-run`, instalação num perfil temporário, `doctor` |

## 11. Riscos e questões abertas

1. **Normalizadores são código de parsing por formato.** Precisam de fixtures reais e anonimizadas de cada provider. Sem elas, o formato não entra.
2. **MCPs que só devolvem no contexto custam tokens** em payloads grandes (diff, design). A orientação é preferir CLI com redirecionamento ou `save_to`.
3. **O design `approx` tem limite de evidência:** medidas estimadas não provam fidelidade de pixel. O gate precisa deixar isso explícito e exigir revisão humana.
4. **O guard por verbo pode ter falso positivo e falso negativo.** É fail-closed, com uma lista explícita para os servidores conhecidos.
5. **O harness fica em GitLab+Jira legado.** A factory é a sucessora e o produto divulgado.

## 12. Fontes

- Stack Overflow Developer Survey 2025: <https://survey.stackoverflow.co/2025/technology/>
- GitHub MCP: <https://github.com/github/github-mcp-server> · `gh pr ready`: <https://cli.github.com/manual/gh_pr_ready>
- GitLab MCP tools: <https://docs.gitlab.com/user/model_context_protocol/mcp_server_tools/>
- Atlassian Rovo MCP tools: <https://developer.atlassian.com/cloud/rovo-mcp/guides/supported-tools/>
- Figma MCP: <https://developers.figma.com/docs/figma-mcp-server/>
- Azure DevOps MCP: <https://github.com/microsoft/azure-devops-mcp/blob/main/docs/TOOLSET.md>
- Linear MCP: <https://linear.app/docs/mcp>
