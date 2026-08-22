# dependency-compat-mcp

**"이 두 버전, 같이 써도 됩니까?"** 에 공식 1차 출처와 함께 답하는 MCP 서버입니다.

정확한 두 릴리스(예: `pypi:django@5.2`와 `runtime:python@3.13`)를 받아 `supported` / `unsupported` / `unknown` 중 하나와 **그 결론이 딛고 선 출처**를 구조화해 돌려줍니다. 요청을 처리하는 동안 모델을 호출하지 않으므로, 답을 지어낼 자리가 구조적으로 없습니다.

```bash
uvx --python 3.14 dependency-compat-mcp                     # 로컬 stdio
uvx --python 3.14 dependency-compat-mcp --transport http    # 원격 무상태 Streamable HTTP
```

---

## 목차

- [왜 필요한가](#왜-필요한가)
- [다른 방법과 비교](#다른-방법과-비교)
- [설치와 등록](#설치와-등록)
- [무엇을 물어볼 수 있는가](#무엇을-물어볼-수-있는가)
- [도구 1 — `check_compatibility`](#도구-1--check_compatibility)
- [도구 2 — `get_compatibility_context`](#도구-2--get_compatibility_context)
- [응답 읽는 법](#응답-읽는-법)
- [동작 원리](#동작-원리)
- [참조: 응답에 나올 수 있는 값](#참조-응답에-나올-수-있는-값)
- [제한사항](#제한사항)
- [운영 한계](#운영-한계)
- [개발과 기여](#개발과-기여)

---

## 왜 필요한가

"Django 5.2가 asgiref 3.7.2와 함께 설치됩니까?" 같은 질문에 지금 AI 에이전트가 답하는 방법은 셋뿐이고, 셋 다 이 질문에는 약합니다.

**모델의 기억으로 답한다.** 학습 컷오프 이후 릴리스는 존재조차 모르고, 틀렸을 때도 맞았을 때와 같은 어조로 답합니다. 특히 `>=3.8.1`이 `3.8.1`을 포함하는지, `^6.0.0`이 `6.0.0`을 포함하고 `7.0.0-0`을 배제하는지 같은 **경계 판정**은 기억으로 답하기 가장 나쁜 종류입니다.

**웹 검색으로 답한다.** 검색 결과에는 게시자의 공식 선언과 3년 전 블로그 글과 누군가의 이슈 코멘트가 같은 무게로 섞여 옵니다. 대부분은 "Django + Python" 수준이지 "Django 5.2 + Python 3.13"이 아니며, 응답만 봐서는 결론이 어느 문장에서 나왔는지 되짚을 수 없습니다.

**문서 MCP(Context7 등)로 답한다.** 라이브러리 문서를 정확히, 최신으로 가져옵니다. 다만 가져온 것은 **텍스트**입니다. 그 텍스트가 물어본 정확한 릴리스에 해당하는지 확인하는 일과, `>=3.8.1`을 PEP 440 규칙으로 `3.7.2`와 비교하는 일은 여전히 모델의 추론에 남습니다.

이 서버는 네 번째 방법입니다.

1. **정확한 버전만 받습니다.** 범위(`>=3.10`)나 코드베이스는 입력 계약이 거부합니다. 질문이 모호할 여지를 입구에서 없앱니다.
2. **1차 출처만 읽습니다.** PyPI JSON API, npm registry, CPython·Node.js의 릴리스·EOL 표, 그리고 사람이 공식 문서와 대조해 저장소에 커밋한 큐레이션 팩. 블로그와 포럼은 애초에 소스 목록에 없습니다.
3. **비교는 생태계 표준 파서가 합니다.** PEP 440/508은 `packaging`, npm SemVer는 `node-semver`. 모델도, 직접 짠 문자열 비교도 아닙니다.
4. **결론과 근거를 분리해 돌려줍니다.** `verdict`와, 그 판정을 **직접** 지지하는 `verdict_evidence_ids`와, 확인은 됐지만 판정을 바꾸지 않는 `notices`와, 아예 확인하지 못한 `limitations`가 각각 다른 필드입니다.
5. **모르면 모른다고 말합니다.** `unknown`은 실패가 아니라 1급 결과이고, 왜 모르는지가 코드로 함께 옵니다.

같은 입력과 같은 외부 사실은 같은 결과를 냅니다. 이 성질은 `retrieved_at`을 제외한 바이트 비교 테스트로 검증됩니다.

---

## 다른 방법과 비교

|                     | 웹 검색               | 문서 MCP (Context7 등) | resolver (`uv`/`pip`/`npm`) | **dependency-compat-mcp**                               |
| ------------------- | --------------------- | ---------------------- | --------------------------- | ------------------------------------------------------- |
| 답의 형태           | 산문                  | 문서 발췌              | 설치 성공/실패              | `supported`/`unsupported`/`unknown` + 출처 카탈로그     |
| 정확한 버전 쌍      | 대개 아님             | 문서 단위              | 예                          | 예 (범위 입력은 거부)                                   |
| 버전 경계 비교 주체 | 모델                  | 모델                   | resolver                    | `packaging` / `node-semver`                             |
| 근거 추적           | 링크 품질에 의존      | 문서 출처              | 없음                        | `evidence[].url` + `verdict_evidence_ids`               |
| 무엇을 열었는지     | 알 수 없음            | 알 수 없음             | 알 수 없음                  | `sources_checked` (`ok`/`not_found`/`failed`/`skipped`) |
| 재현성              | 검색 순위에 따라 변동 | 문서 갱신에 따라       | 인덱스 상태에 따라          | 같은 사실 → 같은 결과                                   |
| "모름"의 표현       | 대개 그럴듯한 추측    | "문서 없음"            | 실패 메시지                 | `unknown` + 사유 코드 + 다음 확인 지점                  |
| 전이 의존성 해석    | 아니오                | 아니오                 | **예 (이게 본업)**          | 아니오                                                  |
| 프로젝트 환경 필요  | 아니오                | 아니오                 | 예                          | 아니오                                                  |
| 커버리지            | 넓음                  | 넓음                   | 생태계 전체                 | 좁음 — 아래 네 관계로 한정                              |

**대체 관계가 아닙니다.** 질문의 종류에 따라 도구를 고르십시오.

| 질문                                       | 알맞은 도구                         |
| ------------------------------------------ | ----------------------------------- |
| "이 정확한 두 버전, 같이 써도 됩니까?"     | **이 서버**                         |
| "프로젝트 전체가 설치됩니까?"              | resolver (`uv lock`, `npm install`) |
| "이 API를 어떻게 씁니까?"                  | 문서 MCP (Context7 등)              |
| "왜 안 되죠? 다른 사람들은 어떻게 했나요?" | 웹 검색                             |

이 서버가 나머지보다 나은 지점은 **첫 번째 줄 하나**입니다. 대신 그 한 줄에서는 답이 결정적이고, 근거가 링크로 남고, 모를 때 모른다고 말합니다.

---

## 설치와 등록

### 요구 사항

- **Python 3.14 이상**
- **MCP `2026-07-28`을 말하는 클라이언트.** 이 서버는 이 개정판 **하나만** 구현하며, 이전 핸드셰이크 요청은 `-32022`로 거절합니다. 구버전으로 조용히 되돌려 협상하지 않습니다.
- **아웃바운드 HTTPS**: `pypi.org`, `registry.npmjs.org` (하드코딩된 허용 호스트 전부)

### stdio (로컬, 권장)

MCP 클라이언트 설정에 다음을 추가합니다.

```json
{
  "mcpServers": {
    "dependency-compat": {
      "command": "uvx",
      "args": ["--python", "3.14", "dependency-compat-mcp"]
    }
  }
}
```

Claude Code에서는 한 줄로도 됩니다.

```bash
claude mcp add dependency-compat -- uvx --python 3.14 dependency-compat-mcp
```

저장소에서 직접 실행하려면:

```bash
git clone https://github.com/dongju93/dependency-compat-mcp.git
cd dependency-compat-mcp
uv sync
uv run dependency-compat-mcp
```

### Streamable HTTP (원격)

```bash
uv run dependency-compat-mcp --transport http --host 127.0.0.1 --port 8000
```

`POST /mcp` 하나로 서비스하며 **무상태**입니다. 세션에 판정 상태를 두지 않으므로 인스턴스를 그냥 늘려도 됩니다.

```json
{
  "mcpServers": {
    "dependency-compat": {
      "type": "http",
      "url": "http://127.0.0.1:8000/mcp"
    }
  }
}
```

> **DNS rebinding 방어가 켜져 있습니다.** 허용 Host/Origin은 `--host`·`--port`로 준 값에서 그대로 만들어집니다. 리버스 프록시 뒤에 두어 클라이언트가 다른 Host 헤더를 보낸다면 그 요청은 거부됩니다. 공개 노출에는 TLS와 MCP authorization을 배포 경계에서 처리하십시오. 기본 바인드가 루프백인 것은 의도입니다.

### CLI 옵션

| 옵션          | 기본값      | 설명                                                                   |
| ------------- | ----------- | ---------------------------------------------------------------------- |
| `--transport` | `stdio`     | `stdio` \| `http`                                                      |
| `--host`      | `127.0.0.1` | `--transport http`의 바인드 주소 겸 허용 Host                          |
| `--port`      | `8000`      | `--transport http`의 포트                                              |
| `--log-level` | `INFO`      | 로그는 **항상 stderr**로 나갑니다 (stdio에서 stdout은 프로토콜 와이어) |

### 클라이언트가 알아야 할 프로토콜 동작

SDK 기본값을 그대로 두지 않은 곳이 네 군데 있고, 모두 "광고한 것과 실제 동작이 같아야 한다"는 한 가지 이유에서 나왔습니다. 클라이언트 입장에서 체감되는 차이는 다음과 같습니다.

| 동작                          | 클라이언트가 겪는 일                                                                                                   |
| ----------------------------- | ---------------------------------------------------------------------------------------------------------------------- |
| `2026-07-28`만 서빙           | 다른 개정판으로 핸드셰이크하면 `-32022` 오류. 구버전 폴백 없음                                                         |
| Tools만 광고                  | `prompts/*`, `resources/*`는 빈 목록이 아니라 `METHOD_NOT_FOUND`                                                       |
| 최상위 인자 객체를 닫음       | `subject`/`counterpart`/`target` 외의 키는 거부. `tools/list`의 `inputSchema`에도 `additionalProperties: false`로 광고 |
| 출력 스키마의 `$ref` 인라인화 | `oneOf`와 판별자(`verdict`, `availability`)가 스키마 루트에서 바로 보임                                                |

알 수 없는 인자는 JSON-RPC 예외가 아니라 `is_error: true`인 `CallToolResult`로 돌아옵니다. 중첩 필드 위반을 SDK가 이미 그 모양으로 반환하기 때문이며, 같은 실수가 한쪽은 프로토콜 오류로 다른 쪽은 도구 결과로 도착하는 상황을 만들지 않기 위해서입니다.

---

## 무엇을 물어볼 수 있는가

모든 대상은 세 값으로만 식별합니다.

```text
TargetInput = namespace + name + exact version
```

| namespace | 허용 `name`      | 버전 문법        |
| --------- | ---------------- | ---------------- |
| `pypi`    | PyPI 프로젝트명  | PEP 440          |
| `npm`     | npm 패키지명     | SemVer (strict)  |
| `runtime` | `python`, `node` | 각 런타임 릴리스 |

판정 가능한 관계는 넷입니다. 이 표에 없는 조합은 **소켓을 열기 전에** `unknown / relation_not_supported`로 끝납니다.

| 관계                      | 읽는 선언                                    | 방향 정책        |
| ------------------------- | -------------------------------------------- | ---------------- |
| `pypi` × `runtime:python` | 패키지의 `requires_python`                   | 역방향 해석 허용 |
| `pypi` × `pypi`           | subject의 `requires_dist`                    | 입력 순서 유지   |
| `npm` × `runtime:node`    | 패키지의 `engines.node`                      | 역방향 해석 허용 |
| `npm` × `npm`             | subject의 `dependencies`, `peerDependencies` | 입력 순서 유지   |

**받지 않는 입력** — 아래는 모두 도구 오류입니다. 조용히 보정하지 않습니다.

- 버전 범위 (`>=3.10,<3.14`, `^19`) — 정확한 버전 하나만
- 접두사 붙은 버전 (`v22.17.0`) — `22.17.0`으로 고쳐주지 않습니다
- 저장소 경로, 소스 코드, manifest, lockfile
- 임의 URL, 검색어
- `maven`, `runtime:ruby`, 그 밖에 등록되지 않은 namespace

---

## 도구 1 — `check_compatibility`

방향이 있는 두 대상 질문입니다.

```json
{
  "subject": { "namespace": "pypi", "name": "django", "version": "5.2" },
  "counterpart": { "namespace": "pypi", "name": "asgiref", "version": "3.8.1" }
}
```

응답(실제 출력에서 발췌):

```json
{
  "verdict": "supported",
  "subject": { "namespace": "pypi", "name": "django", "version": "5.2" },
  "counterpart": { "namespace": "pypi", "name": "asgiref", "version": "3.8.1" },
  "relation": {
    "status": "resolved",
    "rule": "requires_dist",
    "direction": "as_given",
    "declaring": { "namespace": "pypi", "name": "django", "version": "5.2" },
    "declared_about": {
      "namespace": "pypi",
      "name": "asgiref",
      "version": "3.8.1"
    }
  },
  "summary": "pypi:asgiref 3.8.1 satisfies pypi:django 5.2's requires_dist, backed by 1 source(s).",
  "evidence": [
    {
      "id": "evidence-1",
      "source_type": "registry_metadata",
      "title": "Distribution metadata for django 5.2",
      "url": "https://pypi.org/project/django/5.2/",
      "substantiates": "django 5.2 declares Requires-Dist: asgiref>=3.8.1.",
      "expression": ">=3.8.1",
      "scheme": "pep440",
      "provenance": {
        "kind": "fetched",
        "retrieved_at": "2026-08-22T08:41:28Z"
      }
    }
  ],
  "notices": [],
  "limitations": [{ "code": "curated_pack_missing" }],
  "sources_checked": [
    {
      "source": "pypi_json",
      "target": { "namespace": "pypi", "name": "django", "version": "5.2" },
      "role": "declaring",
      "required": true,
      "outcome": "ok",
      "detail": null
    },
    {
      "source": "curated_pack",
      "target": { "namespace": "pypi", "name": "django", "version": "5.2" },
      "role": "declaring",
      "required": false,
      "outcome": "not_found",
      "detail": null
    },
    {
      "source": "pypi_json",
      "target": { "namespace": "pypi", "name": "asgiref", "version": "3.8.1" },
      "role": "declared_about",
      "required": true,
      "outcome": "ok",
      "detail": null
    }
  ],
  "verdict_evidence_ids": ["evidence-1"]
}
```

`sources_checked`는 **소스별 요약이 아니라 실제 조회 단위의 목록**입니다. 위 응답은 `pypi_json`을 두 번 읽었고 두 릴리스가 각각 존재함을 확인했다는 사실을 그대로 보여줍니다. 한 줄로 합치면 어느 쪽이 확인된 것인지 알 수 없습니다. `role`은 입력 순서가 아니라 **선언 방향**을 가리키므로, `direction`이 `reversed`인 응답에서도 `declaring`은 실제로 선언한 쪽입니다.

`required`는 그 조회 실패가 판정을 막는지 여부입니다. 두 릴리스의 존재는 모두 판정의 전제이므로 양쪽 레지스트리 조회가 `required: true`이고, 실패하면 `release_not_found`가 아니라 `lookup_failed`가 됩니다. **"조회하지 못했다"를 "존재하지 않는다"로 바꾸지 않습니다.**

같은 선언에 `asgiref 3.7.2`를 넣으면 `verdict`가 `unsupported`로 바뀌고, `summary`가 `"pypi:django 5.2 excludes pypi:asgiref 3.7.2 via requires_dist"`가 되며, **evidence는 같은 한 줄을 가리킵니다.** 판정을 뒤집은 것이 어느 선언인지 그대로 보입니다.

### 세 가지 결과

| 결과          | 의미                                           |
| ------------- | ---------------------------------------------- |
| `supported`   | 정확한 버전 조합을 지지하는 명시적 근거가 있음 |
| `unsupported` | 정확한 버전 조합을 배제하는 명시적 근거가 있음 |
| `unknown`     | 현재 근거로 어느 쪽도 증명할 수 없음           |

`supported`와 `unsupported`에는 `verdict_evidence_ids`가 **최소 하나** 있어야 합니다. 타입 수준에서 강제되므로 "근거 없는 판정"은 만들어질 수 없습니다. 문서에 상대 버전이 없다는 사실만으로 `unsupported`를 만들지 않고, 비호환 근거를 못 찾았다는 이유로 `supported`를 만들지도 않습니다.

### 인자 순서가 곧 질문입니다

`pypi` × `runtime:python`처럼 **선언 주체가 대상의 종류로 정해지는** 관계는 인자를 반대로 넣어도 같은 규칙으로 읽고, `relation.direction`을 `reversed`로 보고합니다.

반대로 `pypi A` × `pypi B`의 `requires_dist`는 **입력 순서가 선언 주체를 정합니다.** `A → B`와 `B → A`는 다른 질문이고 verdict도 다를 수 있으므로 순서를 뒤집지 않습니다.

`relation`은 `resolved | unsupported` 합 타입입니다. 규칙을 찾지 못한 경우 입력 쌍만 보존하며, 존재하지 않는 규칙이나 선언 주체를 `null`로 꾸며내지 않습니다.

---

## 도구 2 — `get_compatibility_context`

판정이 아니라 **비교 재료**를 돌려줍니다. 코드베이스와 대조하는 일은 클라이언트의 몫입니다.

```json
{ "target": { "namespace": "pypi", "name": "django", "version": "5.2" } }
```

응답에는 확인된 범위만큼 다음이 담깁니다.

- 다른 패키지·런타임에 대한 `requires` / `supports` / `excludes` 조건과, 그 조건이 무조건 적용되는지 환경 marker나 선택된 extra에 걸려 있는지를 말하는 `condition`
- `breaking_change` / `removal` / `deprecation` / `migration_required` 변경 사항
- 각 사실이 참조하는 공식 근거
- 수집하지 못했거나 정확한 버전에 대해 검증하지 못한 범위

`verdict`가 없는 대신 최상위에 판별자 두 개가 이 순서로 옵니다.

| 필드           | 값                                        | 읽는 법                                    |
| -------------- | ----------------------------------------- | ------------------------------------------ |
| `availability` | `available` \| `unknown`                  | 비교 재료를 하나라도 찾았는가              |
| `depth`        | `registry_only` \| `registry_and_curated` | 그 재료에 사람이 검토한 근거가 섞여 있는가 |

`available`은 `constraints`나 `changes`가 **반드시 하나 이상** 있다는 뜻이고, `unknown`은 둘 다 **반드시 비어 있다**는 뜻입니다. 양방향 모두 타입이 강제하므로 "available인데 본문이 빈" 응답은 존재할 수 없습니다. `unknown`일 때는 `reason`이 함께 옵니다.

각 `constraint`의 `condition`은 `explanation` 문장이 아니라 필드입니다. 이 도구는 판정하지 않으므로 "이 제약이 내 환경에 걸리는가"는 클라이언트만 답할 수 있고, 클라이언트는 필드로만 답할 수 있습니다.

`depth`는 카탈로그 전체가 아니라 **실제로 인용된** 근거에서 계산합니다. 아무것도 기여하지 못한 큐레이션 항목이 깊이를 광고하지 못하게 하기 위해서입니다. `registry_only`라면 그 응답은 패키지 메타데이터의 구조화 재전달이며, 본문을 파싱하기 전에 추가 조사가 필요한지 판단할 수 있습니다.

---

## 응답 읽는 법

응답은 **결론, 근거, 확인된 부가 사실, 확인하지 못한 범위**를 서로 다른 필드로 표현합니다. 이 구분이 이 서버의 핵심 산출물입니다.

| 필드                   | 담는 것                                                            | 예                                                 |
| ---------------------- | ------------------------------------------------------------------ | -------------------------------------------------- |
| `evidence`             | verdict, constraint, change, notice가 참조하는 출처 카탈로그       | `requires_python` 제약, 공식 지원 정책 문서        |
| `verdict_evidence_ids` | `supported`/`unsupported`를 **직접** 지지하는 evidence ID 부분집합 | 설치를 허용하거나 거부한 제약의 evidence ID        |
| `notices`              | **확인된** 사실 중 verdict를 바꾸지 않는 것                        | 릴리스가 yanked됨, 메타데이터와 공식 문서의 불일치 |
| `decision_causes`      | `unknown`을 **만든** 원인 (`insufficient_evidence`일 때만)         | 환경 marker로 가드된 선언, 열린 상한, tier C 단독  |
| `limitations`          | **확인하지 못한** 범위                                             | 큐레이션 항목 없음, 소스 조회 실패                 |
| `sources_checked`      | 어느 대상에 대해 무엇을 열었고 결과가 무엇이었는지                 | django 5.2의 `pypi_json: ok`, asgiref 3.8.1의 `ok` |
| `summary`              | 구조화 결과를 그대로 옮긴 한 줄 요약                               | 판정·규칙·근거 개수, 판정을 막은 첫 번째 원인      |

예를 들어 설치 게이트는 버전을 거부하는데 공식 지원 문서는 허용한다고 말할 수 있습니다. 이때 두 출처를 모두 `evidence`에 보존하되, `verdict_evidence_ids`는 `unsupported`를 직접 지지하는 게이트만 가리키고, 불일치는 `notices`가 관련 evidence ID를 참조합니다. **한쪽을 지우지 않습니다.**

`decision_causes`와 `limitations`는 서로 다른 질문에 답합니다. `limitations`는 **커버리지**입니다 — 큐레이션 팩이 없다는 사실은 거의 모든 응답에 붙지만 그 자체가 판정을 막은 원인은 아닙니다. `decision_causes`는 바로 이 응답을 `unknown`으로 만든 것이고, 그래서 원인마다 자신의 `evidence_ids`와, 있다면 marker 원문을 함께 싣습니다. `supported`/`unsupported`에는 이 필드가 **아예 없습니다.**

`notices`, `limitations`, `decision_causes[].kind`는 자유 문장이 아니라 **닫힌 코드 집합**이며, 모델이 쓴 완곡 표현이 아니라 계산 결과입니다. `summary`도 같은 원칙을 따릅니다 — 문장을 작성하는 코드 경로가 아예 없고, `(판정, 규칙, 방향, 개수, 첫 번째 decision cause)`로 닫힌 템플릿 목록에서 하나를 고를 뿐입니다. `unknown / insufficient_evidence`의 문장은 독립적인 두 번째 설명이 아니라 `decision_causes[0]`의 투영이므로, summary가 구조화 결과에 없는 원인을 말할 수 없습니다. 최종 사용자용 표현과 언어는 MCP 클라이언트의 몫이므로 이 문자열은 영어 기계 판독용으로 남습니다.

### `unknown`을 받았을 때

```json
{
  "verdict": "unknown",
  "reason": "insufficient_evidence",
  "summary": "pypi:django 5.2's requirement for pypi:tzdata 2025.2 is conditional on sys_platform == \"win32\"; the request carries no environment, so compatibility is unknown.",
  "decision_causes": [
    {
      "kind": "conditional_claim",
      "condition": {
        "kind": "environment_marker",
        "expression": "sys_platform == \"win32\"",
        "variables": ["sys_platform"]
      },
      "evidence_ids": ["evidence-1"]
    }
  ],
  "evidence": [
    {
      "id": "evidence-1",
      "source_type": "registry_metadata",
      "url": "https://pypi.org/project/django/5.2/",
      "substantiates": "django 5.2 declares Requires-Dist: tzdata; sys_platform == \"win32\"."
    }
  ],
  "limitations": [
    { "code": "curated_pack_missing" },
    { "code": "marker_guarded_claim" }
  ]
}
```

Django 5.2의 tzdata 의존성은 `sys_platform == "win32"`일 때만 적용됩니다. 요청에 운영체제 정보가 없으므로 서버는 **찍지 않습니다.** 그 사실이 어디에 있는지가 이 응답의 요점입니다.

- `decision_causes[0].condition.expression`이 레지스트리 원문 그대로의 marker입니다. 서버가 다시 쓰거나 요약하지 않습니다.
- `variables`는 그 marker를 결정지을 환경 변수 이름입니다. 무엇을 알아내면 답이 정해지는지가 필드로 나옵니다.
- `evidence_ids`는 같은 응답의 `evidence[].id`로 반드시 해소됩니다. 원인과 출처가 끊어질 수 없습니다.
- `summary`는 이 cause를 렌더링한 결과일 뿐, 별도로 작성된 문장이 아닙니다.

`unknown`에는 `verdict_evidence_ids` 필드 자체가 없습니다 — 지지할 판정이 없기 때문입니다. 반대로 `decision_causes`는 `reason`이 `insufficient_evidence`일 때 **반드시 하나 이상** 있고, 그 밖의 `reason`에는 **절대 없습니다**. 나머지 `reason`(`lookup_failed`, `release_not_found`, `conflicting_evidence`, `no_declared_relationship` 등)은 이름 자체가 이미 원인이기 때문입니다.

원인이 둘 이상이면 고정된 우선순위의 첫 번째만 `summary`에 문장으로 나오고 나머지는 개수로 덧붙습니다. **전체 목록은 항상 `decision_causes`에 남습니다.**

읽는 순서를 권한다면:

1. `verdict` 또는 `availability`
2. `unknown`이면 `reason` → `decision_causes` → `limitations` → `sources_checked` (다음에 무엇을 확인할지가 여기 있습니다)
3. 사용자에게 근거를 보여줄 때는 `evidence[].url`을 그대로 인용

---

## 동작 원리

```text
도구 입력
  -> Target 파싱 (namespace별 파서, round-trip 강제)
  -> 관계 규칙 해석 (canonicalizable | directional)
       -> 미지원 관계면 외부 조회 없이 unknown / relation_not_supported
  -> 레지스트리 메타데이터와 큐레이션 근거 수집  ← 유일한 I/O 구간
  -> 원시 응답을 Claim과 Evidence로 파싱
  -> 순수 총함수 evaluate(...)로 판정
  -> 근거 참조 무결성을 검사한 구조화 출력
```

판정 함수는 네트워크, MCP 타입, 현재 시각, 난수, 모델 호출을 **알지 못합니다.** I/O는 어댑터에 격리되고, 판정은 같은 사실에 대해 같은 결과를 내는 결정적 함수로 남습니다. 두 조회는 하나의 `TaskGroup` 안에서 15초 요청 예산 아래 함께 실행됩니다.

### 인정하는 근거

| 티어  | 근거                                                                                        | 역할                         |
| ----- | ------------------------------------------------------------------------------------------- | ---------------------------- |
| **A** | 설치·관계 게이트: `requires_python`, `requires_dist`, npm `dependencies`·`peerDependencies` | 지원·비지원 판정 가능        |
| **B** | npm `engines.node`, 사람이 검토해 커밋한 공식 지원 정책·릴리스 노트                         | 지원·비지원 판정 가능        |
| **C** | PyPI classifier 등 열거형 긍정 신호                                                         | 다른 근거의 지원 판정만 보강 |

### 판정 원칙

- **티어 A 게이트 위반은 항상 `unsupported`입니다.** 명시적 호환성 진술이 반대여도 게이트가 이깁니다. 반대 진술은 `evidence`에 보존하고 불일치를 `gate_contradicts_statement` notice로 보고하되, `verdict_evidence_ids`는 게이트만 가리킵니다.
- npm `engines.node`는 기본적으로 **경고**이고 `engine-strict`일 때만 설치를 막으므로 게이트로 취급하지 않습니다. 게시자의 명시적 진술로 다룹니다.
- **상한이 없는 제약을 "미래의 모든 릴리스 지원"으로 확대 해석하지 않습니다.** 같은 회의를 하한에도 적용해, 선언 시점에 이미 EOL이던 런타임에 대해서도 보강 근거를 요구합니다.
- 명시적 진술끼리 충돌하면 한쪽을 고르지 않고 `unknown / conflicting_evidence`를 반환합니다.
- 근거 부재, 부분 근거, 조회 실패, 선언된 관계 없음, 미지원 관계는 실패를 숨기지 않고 각각의 `unknown` 사유로 구분해 반환합니다. `insufficient_evidence`만은 사유 이름이 곧 원인이 되지 못하므로, 구조화된 `decision_causes` 없이는 아예 만들어지지 않습니다.
- 버전 문법을 생태계 사이에서 **번역하지 않습니다.** 스킴이 다르면 `None`이지, 억지로 맞춘 비교가 아닙니다.

### 오류와 `unknown`의 차이

| 구분          | 예시                                                                                       |
| ------------- | ------------------------------------------------------------------------------------------ |
| **도구 오류** | 필수 필드 누락, 알 수 없는 필드, 등록되지 않은 namespace, 범위 버전 입력, 내부 불변식 위반 |
| **`unknown`** | 릴리스 미발견, 근거 부족·충돌, 외부 조회 실패, 선언된 관계 없음, 미지원 관계               |

`unknown`은 약한 성공도 약한 실패도 아니라 "현재 근거로는 증명할 수 없음"이라는 독립된 도메인 결과입니다. 재시도할 이유가 아니며, `decision_causes`가 무엇이 판정을 막았는지를, `limitations`와 `sources_checked`가 다음 확인 지점을 알려줍니다.

---

## 참조: 응답에 나올 수 있는 값

프로그램으로 응답을 다룰 때 필요한 **닫힌 집합 전부**입니다. 여기 없는 문자열은 나오지 않습니다.

| 필드                        | 가능한 값                                                                                                                                                             |
| --------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `verdict`                   | `supported`, `unsupported`, `unknown`                                                                                                                                 |
| `reason` (verdict)          | `release_not_found`, `lookup_failed`, `relation_not_supported`, `conflicting_evidence`, `insufficient_evidence`, `evidence_not_found`, `no_declared_relationship`     |
| `availability`              | `available`, `unknown`                                                                                                                                                |
| `reason` (context)          | `release_not_found`, `lookup_failed`, `evidence_not_found`                                                                                                            |
| `depth`                     | `registry_only`, `registry_and_curated`                                                                                                                               |
| `relation.status`           | `resolved`, `unsupported`                                                                                                                                             |
| `relation.rule`             | `requires_python`, `requires_dist`, `engines_node`, `npm_dependency`                                                                                                  |
| `relation.direction`        | `as_given`, `reversed`                                                                                                                                                |
| `notices[].code`            | `subject_yanked`, `counterpart_yanked`, `gate_contradicts_statement`                                                                                                  |
| `limitations[].code`        | `curated_pack_missing`, `curated_not_verified_for_version`, `marker_guarded_claim`, `extra_guarded_claim`, `source_unavailable`                                       |
| `decision_causes[].kind`    | `conditional_claim`, `open_upper_bound`, `stale_lower_bound`, `tier_c_only`, `claim_outside_range`, `uncomparable_claim` (이 순서가 곧 `summary` 선택 우선순위입니다) |
| `condition.kind`            | `unconditional`, `environment_marker`, `extra_marker`, `decided_marker`                                                                                               |
| `evidence[].source_type`    | `registry_metadata`, `registry_classifier`, `official_support_policy`, `official_release_note`                                                                        |
| `sources_checked[].source`  | `pypi_json`, `npm_registry`, `curated_pack`, `python_release_table`, `node_release_table`                                                                             |
| `sources_checked[].role`    | `declaring`, `declared_about`                                                                                                                                         |
| `sources_checked[].outcome` | `ok`, `not_found`, `failed`, `skipped`                                                                                                                                |
| `constraints[].relation`    | `requires`, `supports`, `excludes`                                                                                                                                    |
| `changes[].category`        | `breaking_change`, `removal`, `deprecation`, `migration_required`                                                                                                     |

`decision_causes[].condition`은 `conditional_claim`에만 있고, `environment_marker`와 `extra_marker` 둘 중 하나입니다 — "무언가에 조건부이긴 한데 무엇인지는 없음"이 스키마상 표현되지 않습니다.

`constraints[].condition`은 실제로는 `unconditional`, `environment_marker`, `extra_marker` 세 가지만 나옵니다. `decided_marker`(환경 변수를 하나도 이름하지 않아 한 번의 평가로 결정되는 marker, `holds`에 그 진리값)는 타입으로는 존재하지만 **현재 레지스트리 메타데이터로는 도달하지 않습니다.** 유효한 PEP 508 marker는 반드시 정의된 변수를 하나 이상 이름하므로 그 전에 `environment_marker`로 분류되고, 변수를 이름하지 않는 표현식(`"a" == "a"` 등)은 `packaging`이 한쪽을 환경 키로 읽어 환경 없이 평가되지 않습니다. 이 사실은 테스트로 고정돼 있어, 나중에 도달 가능해지면 리뷰를 거치게 됩니다.

`sources_checked`가 비어 있는 응답은 `reason: "relation_not_supported"` 하나뿐입니다. 규칙이 없어 소켓을 하나도 열지 않았다는 뜻이고, 그 반대도 성립합니다(다른 어떤 `reason`으로도 빈 목록이 나오지 않습니다).

---

## 제한사항

이 서버가 **하지 않는 일**과 **잘 못하는 일**입니다. 도입 전에 읽어야 할 절입니다.

### 1. 큐레이션 팩이 비어 있는 상태로 배포됩니다

로더, 스키마 검증, 조회, 출력 경로는 모두 구현되고 실제 항목 fixture로 테스트되지만, 저장소에 커밋된 팩(`curated/pack/compatibility.json`)의 `entries`는 **비어 있습니다**. 사람이 공식 출처와 대조해 리뷰한 근거만 팩에 들어갈 수 있고, 구현 과정에서 근거를 지어내지 않았기 때문입니다.

따라서 **지금 이 서버의 모든 응답은 `depth: registry_only`이고 `limitations`에 `curated_pack_missing`이 붙습니다.** 버그가 아니라 정확한 상태 보고입니다.

**런타임 릴리스 표는 다릅니다.** `curated/runtime_releases.json`은 2026-08-12 기준 CPython 461개와 Node.js 860개 릴리스의 출시일·EOL을 담은 완전한 스냅샷이며, python.org 다운로드 API, peps.python.org 릴리스 사이클, nodejs.org dist 인덱스, nodejs/Release 일정표 네 곳의 1차 출처에서 생성됩니다. EOL이 `YYYY-MM`으로만 공표된 라인은 `eol_at: null`입니다 — 월을 일자로 바꾸면 없는 사실을 만드는 것이기 때문입니다.

### 2. 실질 가치는 큐레이션 팩 커버리지와 같습니다

이 서버만 제공할 수 있는 것은 **공식적으로 검증된 지원 선언**과 **`changes`(breaking change, removal, deprecation, migration)** 이며, 둘 다 사람이 검토해 커밋한 큐레이션 팩에서만 나옵니다. 팩에 항목이 없는 대상에 대해 이 서버는 레지스트리 메타데이터를 구조화해 재전달할 뿐입니다.

커버리지를 넓히는 유일한 방법은 팩에 항목을 추가하는 것이고, 그것은 사람의 리뷰 노동입니다. 설계상의 거래입니다 — **요청 경로에서 LLM을 배제한 대가로 커버리지를 잃고 근거의 검증 가능성을 얻었습니다.**

### 3. 의존성 resolver보다 정확하지 않습니다

subject가 선언한 **직접** 제약만 봅니다. 전이 의존성, 버전 해석, 충돌 해소는 하지 않습니다. 프로젝트 전체가 설치 가능한지 알고 싶다면 해당 생태계의 resolver가 정확합니다.

이 서버가 그 위에 더하는 것은 "선언된 제약이 무엇이고 그 근거가 어디에 있는가"입니다. **해결이 아니라 설명입니다.**

### 4. `unknown`이 자주 나옵니다

설계 의도지만 기대치를 맞춰 두는 편이 낫습니다. 다음 경우에 나옵니다.

- `requires_python`에 상한이 없고, 물어본 런타임이 그 패키지 릴리스보다 나중에 나왔으며, 큐레이션 근거가 없을 때 (`open_upper_bound`)
- `requires_python`에 상한이 없고, 물어본 런타임이 그 패키지 릴리스 시점에 이미 EOL이었을 때 (`stale_lower_bound`)
- classifier 같은 긍정 신호만 있고 게이트나 명시적 진술이 없을 때 (`tier_c_only`)
- 환경 marker나 extra로 가드된 조건부 의존성일 때 (`conditional_claim`)
- 명시된 범위가 물어본 버전을 아예 다루지 않을 때 (`claim_outside_range`)
- 선언된 표현식과 버전의 version scheme이 달라 비교 자체가 불가능할 때 (`uncomparable_claim`)

괄호 안이 그대로 `decision_causes[].kind`입니다. **`unknown`은 나오는데 왜 그런지는 알 수 없는 응답이 없습니다** — `insufficient_evidence`는 구조화된 원인 없이는 만들어질 수 없습니다.

"최신 Python + 임의의 패키지" 조합은 상당 비율이 `unknown`이 될 것으로 예상합니다. 이때 응답은 "설치는 되지만 저자가 검증했다는 근거는 없다"를 `evidence`와 `decision_causes`로 나눠 전달합니다. **호환된다고 추측하는 것보다 정직하지만, 확답을 원하는 사용자에게는 답답할 수 있습니다.**

### 5. 코드베이스를 보지 않습니다

저장소 경로, 소스 코드, manifest, lockfile을 입력받지 않습니다. 따라서 "당신의 프로젝트는 호환됩니다"라고 **말할 수 없고 말하지 않습니다.** 그 종합은 MCP 클라이언트의 책임입니다.

환경 marker의 진리값도 같은 이유로 알 수 없습니다. 임의로 참이라 가정하지 않고, marker 원문과 그것을 결정지을 변수 이름을 `decision_causes`(판정 도구)와 `constraints[].condition`(맥락 도구)에 그대로 실어 보고합니다.

### 6. 외부 자료가 사실인지, 최신인지 증명할 수 없습니다

타입과 구조는 응답의 형태를 보장하지만, 레지스트리 메타데이터가 정확한지, 공식 문서가 갱신되었는지, 큐레이션 항목이 여전히 유효한지는 증명하지 못합니다. 서버가 할 수 있는 것은 판단 재료를 남기는 것까지입니다: `provenance.reviewed_at`, `pack_version`, `verified_against`, `sources_checked`.

큐레이션 근거 URL의 생존도 보장되지 않습니다. 예약 CI가 링크 부패를 **검출**하지만 **예방**하지는 못합니다.

### 7. 지원 경계 밖

| 항목                                       | 상태                                 |
| ------------------------------------------ | ------------------------------------ |
| `maven`, `cargo`, 임의 `product` namespace | 등록하지 않음                        |
| 코드베이스 분석, 설치·업그레이드·코드 수정 | 범위 밖                              |
| 전이 의존성 해석, 버전 해결                | 범위 밖 (`uv`/`pip`/`npm` 사용)      |
| 버전 범위 입력 (`>=3.10,<3.14`, `^19`)     | 입력 계약이 거부. 정확한 버전 하나만 |
| 대안 버전 추천 ("3.12를 쓰세요")           | 하지 않음. 서버는 추측하지 않음      |

### 8. 요청 경로에 AI가 없습니다

제한사항이자 보장입니다. 서버는 요청 처리 중 LLM이나 다른 MCP 서버를 호출하지 않습니다. 따라서 근거를 생성하거나 문서를 요약하거나 검색 결과를 종합하지 않습니다. 커버리지가 좁은 대신 같은 입력과 같은 외부 사실은 같은 정규화 결과를 냅니다.

---

## 운영 한계

요청 경로의 외부 접근은 전부 한 곳(`infra/http.py`)을 지나며, 한계값은 관례가 아니라 구조로 강제됩니다.

| 항목            | 값                                                                     |
| --------------- | ---------------------------------------------------------------------- |
| 허용 호스트     | `pypi.org`, `registry.npmjs.org` (하드코딩)                            |
| URL 조립        | 단일 생성자만 사용. **사용자 입력은 URL이 될 수 없음**                 |
| 리다이렉트      | 최대 3홉, **매 홉마다** 호스트 재검증 (클라이언트 자동 추적 비활성)    |
| 응답 본문       | 5 MiB. 스트리밍 중 검사가 기준이고 `Content-Length`는 조기 탈출용일 뿐 |
| 시도당 타임아웃 | 5초 (본문 읽기 포함)                                                   |
| 요청 전체 예산  | 15초                                                                   |
| TTL 캐시        | 900초. 키에 `pack_version`이 포함되어 팩 변경이 캐시를 무효화          |
| HTTP 요청 본문  | 4 MiB (`--transport http`)                                             |

조회 실패는 예외가 아니라 **값**입니다. 예외였다면 스택 어딘가에서 잡혀, 이를 위한 분기를 가진 판정 함수까지 도달하지 못했을 것입니다. 유일한 예외는 취소로, 이것은 레지스트리의 실패가 아니라 호출자가 질문을 철회한 것입니다.

런타임 의존성은 `mcp`, `packaging`, `node-semver` 셋뿐이며 `uv.lock`과 동기화되어 있습니다.

---

## 개발과 기여

```bash
uv run pytest                    # 618 tests, ~6s, 커버리지 91%
uv run pytest -m "not slow" --no-cov
uv tool run ruff check .
uv tool run ruff format --check .
uv tool run pyrefly check        # strict preset, 0 errors 유지
```

가장 가치 있는 기여는 **큐레이션 팩 항목**입니다. 항목 하나가 늘 때마다 `depth: registry_and_curated`로 답할 수 있는 질문이 늘어납니다. 규칙은 `src/dependency_compat_mcp/curated/pack/README.md`에 있으며, 요약하면 이렇습니다.

- 출처는 **읽은 것**이지 기억한 것이 아닙니다. URL을 열어 그 페이지가 실제로 그렇게 말하는지 확인하십시오.
- `reviewed_by`와 `reviewed_at`은 필수입니다. 없으면 아무도 항목의 신선도를 판단할 수 없습니다.
- 진술과 변경 하나마다 출처 하나. `source.url` 없는 항목은 **로드 시점에 거부되고 서버가 뜨지 않습니다.**
- URL 호스트는 허용 목록에 있어야 합니다. 블로그, 포럼, Q&A, 요약 사이트는 근거가 아닙니다.

런타임 릴리스 표는 손으로 고치지 말고 재생성하십시오.

```bash
uv run scripts/build_runtime_releases.py
```

---

## 라이선스

MIT. [LICENSE](LICENSE)를 보십시오.
