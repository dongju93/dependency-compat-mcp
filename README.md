# dependency-compat-mcp

패키지·런타임의 **정확한 두 버전이 함께 사용 가능한지**를 공식 근거와 함께 확인하는 MCP 서버입니다. 단순한 `true`/`false` 대신 `supported`, `unsupported`, `unknown` 중 하나와 그 결론을 뒷받침하는 출처를 구조화해 반환하도록 설계되어 있습니다.

```bash
uv run dependency-compat-mcp                      # 로컬 stdio
uv run dependency-compat-mcp --transport http     # 원격 무상태 Streamable HTTP (기본 127.0.0.1:8000/mcp)
```

## 이 서버가 하는 일

이 서버는 두 가지 질문에 답합니다.

| 도구                        | 질문                                                                   | 입력                     |
| --------------------------- | ---------------------------------------------------------------------- | ------------------------ |
| `check_compatibility`       | 특정 버전의 대상 A가 특정 버전의 대상 B를 지원하는가?                  | `subject`, `counterpart` |
| `get_compatibility_context` | 특정 버전을 코드베이스와 비교할 때 필요한 조건과 변경 사항은 무엇인가? | `target`                 |

모든 대상은 다음 세 값으로 식별합니다.

```text
TargetInput = namespace + name + exact version
```

예를 들어 `pypi:django@5.2`와 `runtime:python@3.13`을 비교할 수 있습니다. 버전 범위, 코드베이스 내용, 파일 경로, 임의 URL, 검색어는 입력받지 않습니다.

서버는 `pypi`, `npm`, `runtime` namespace를 등록하며, `runtime`에서는 `python`과 `node`만 허용합니다.

| 관계                      | 읽는 선언                                    | 방향 정책        |
| ------------------------- | -------------------------------------------- | ---------------- |
| `pypi` × `runtime:python` | 패키지의 `requires_python`                   | 역방향 해석 허용 |
| `pypi` × `pypi`           | subject의 `requires_dist`                    | 입력 순서 유지   |
| `npm` × `runtime:node`    | 패키지의 `engines.node`                      | 역방향 해석 허용 |
| `npm` × `npm`             | subject의 `dependencies`, `peerDependencies` | 입력 순서 유지   |

`maven`, `product`, `runtime:ruby`와 그 밖의 값은 등록하지 않으며 입력 오류로 처리합니다.

## MCP 호출 흐름

```text
MCP 클라이언트
    │
    ├─ server/discover ── 서버 버전과 tools 역량 확인
    ├─ tools/list ─────── 도구 이름과 입출력 JSON Schema 확인
    └─ tools/call ─────── 정확한 대상 버전 전달
                              │
                              v
                      입력 파싱과 관계 선택 (규칙별 방향 정책)
                              │
                              v
                      근거 수집과 정규화
                              │
                              v
                       결정적 규칙 판정
                              │
                              v
                    구조화 결과와 출처 반환
```

MCP Python SDK의 `MCPServer`가 Python 타입 힌트에서 도구 스키마를 만들고, 입력 파싱·직렬화·프로토콜 처리를 담당합니다. 애플리케이션은 MCP 메시지를 직접 조립하지 않고 호환성 도메인 규칙에 집중합니다.

같은 `MCPServer`를 로컬용 `stdio`와 원격용 무상태 Streamable HTTP로 모두 제공합니다. MCP `2026-07-28`에서는 요청마다 프로토콜 버전과 클라이언트 정보가 전달되므로 연결이나 세션에 판정 상태를 저장하지 않습니다.

관련 공식 문서:

- [MCP Specification 2026-07-28](https://modelcontextprotocol.io/specification/2026-07-28)
- [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk)
- [MCP Python SDK - Tools](https://github.com/modelcontextprotocol/python-sdk/blob/main/docs/servers/tools.md)
- [MCP Python SDK - Structured output](https://github.com/modelcontextprotocol/python-sdk/blob/main/docs/servers/structured-output.md)

## 1. 두 버전 직접 비교

`check_compatibility`는 방향이 있는 질문입니다.

```json
{
  "subject": { "namespace": "pypi", "name": "django", "version": "5.2" },
  "counterpart": { "namespace": "runtime", "name": "python", "version": "3.13" }
}
```

이 요청은 "Django 5.2가 Python 3.13을 지원하는가?"를 뜻합니다. 입력은 JSON Schema와 namespace별 파서에서 완전히 파싱된 뒤에만 판정 단계로 이동합니다. 이름과 버전을 추측해서 보정하거나 가까운 릴리스로 대체하지 않습니다.

관계 규칙은 방향 정책을 갖습니다. `pypi` 패키지와 `runtime:python`처럼 선언 주체가 대상 종류로 유일한 관계는 인자 순서를 반대로 넣어도 같은 규칙으로 해석하고, 응답의 resolved `relation.direction`(`as_given` | `reversed`)과 `relation.declaring`으로 실제 해석 방향을 알립니다. 반면 `pypi A -> pypi B`의 `requires_dist`처럼 입력 순서가 선언 주체를 정하는 관계는 순서를 뒤집지 않습니다. `A -> B`와 `B -> A`는 서로 다른 질문이며 verdict도 다를 수 있습니다.

`relation`은 `resolved | unsupported` 합 타입입니다. resolved 관계만 `rule`, `direction`, `declaring`, `declared_about`을 갖습니다. 허용된 방향에서 규칙을 찾지 못한 unsupported 관계는 입력 쌍만 보존하며, 존재하지 않는 규칙이나 선언 주체를 `null`로 꾸며내지 않습니다.

판정 결과는 다음 셋 중 하나입니다.

| 결과          | 의미                                           |
| ------------- | ---------------------------------------------- |
| `supported`   | 정확한 버전 조합을 지지하는 명시적 근거가 있음 |
| `unsupported` | 정확한 버전 조합을 배제하는 명시적 근거가 있음 |
| `unknown`     | 현재 근거로 어느 쪽도 증명할 수 없음           |

`supported`와 `unsupported`에는 verdict를 직접 지지하는 `verdict_evidence_ids`가 최소 하나 존재해야 합니다. 문서에 상대 버전이 없다는 사실만으로 `unsupported`를 만들지 않고, 비호환 근거를 찾지 못했다는 이유만으로 `supported`를 만들지도 않습니다.

### 응답의 근거와 진단 필드

응답은 출처, verdict 근거, 부가 사실과 미확인 범위를 서로 다른 필드로 표현합니다.

| 필드                   | 담는 것                                                          | 예                                                   |
| ---------------------- | ---------------------------------------------------------------- | ---------------------------------------------------- |
| `evidence`             | verdict, constraint, change 또는 notice가 참조하는 출처 카탈로그 | `requires_python` 제약, 공식 지원 정책 문서          |
| `verdict_evidence_ids` | `supported`/`unsupported`를 직접 지지하는 evidence ID의 부분집합 | 설치를 허용하거나 거부한 제약의 evidence ID          |
| `notices`              | **확인된** 사실 중 verdict를 바꾸지 않는 것                      | 릴리스가 yanked됨, 메타데이터와 공식 문서의 불일치   |
| `limitations`          | **확인하지 못한** 범위                                           | 큐레이션 항목 없음, 상한이 열려 있음, 소스 조회 실패 |

예를 들어 설치 게이트가 버전을 거부하지만 공식 지원 문서는 허용한다고 말할 수 있습니다. 이때 두 출처는 모두 `evidence`에 보존하되, `verdict_evidence_ids`는 `unsupported`를 직접 지지하는 설치 게이트만 가리키고 불일치는 `notices`가 관련 evidence ID를 참조합니다.

여기에 `sources_checked`가 더해져, 서버가 어떤 소스를 열었고 결과가 무엇이었는지(`ok` / `not_found` / `failed` / `skipped`)를 알려줍니다. **`unknown`을 받은 호출자가 다음에 무엇을 할지 결정할 수 있어야 하기 때문입니다.**

`notices`와 `limitations`는 자유 문장이 아니라 닫힌 코드 집합이며, 모델이 쓴 완곡 표현이 아니라 계산 결과입니다.

## 2. 코드베이스 비교용 컨텍스트 조회

`get_compatibility_context`는 대상 버전 하나에 관한 비교 재료를 반환합니다.

```json
{
  "target": { "namespace": "pypi", "name": "django", "version": "5.2" }
}
```

응답에는 확인된 범위에 따라 다음 내용이 포함됩니다.

- 다른 패키지·런타임에 대한 `requires`, `supports`, `excludes` 조건
- `breaking_change`, `removal`, `deprecation`, `migration_required` 변경 사항
- 각 사실이 참조하는 공식 근거
- 수집하지 못했거나 정확한 버전에 대해 검증하지 못한 범위

응답 최상위의 **`depth`**가 `registry_only`인지 `registry_and_curated`인지 먼저 알려줍니다. `registry_only`이면 그 응답은 패키지 메타데이터의 구조화 재전달이며, 사람이 검토한 공식 진술이나 변경 사항은 들어 있지 않다는 뜻입니다. 호출자는 본문을 파싱하기 전에 추가 조사가 필요한지 판단할 수 있습니다.

서버는 사용자의 코드베이스를 받거나 읽지 않습니다. MCP 클라이언트가 서버의 구조화된 사실을 manifest, lockfile, 소스 코드와 비교해 최종 설명을 만듭니다.

## 판정 과정

```text
도구 입력
  -> Target 파싱
  -> 관계 규칙 해석 (canonicalizable | directional 정책)
       -> 미지원 관계면 외부 조회 없이 unknown / relation_not_supported
  -> 레지스트리 메타데이터와 큐레이션 근거 수집
  -> 원시 응답을 Claim과 Evidence로 파싱
  -> 유효한 EvaluationInput에 대한 순수 총함수 evaluate(...)로 판정
  -> 근거 참조 무결성을 검사한 구조화 출력 생성
```

판정 함수는 네트워크, MCP 타입, 현재 시각, 난수, 모델 호출을 알지 못합니다. I/O는 어댑터에 격리하고, 판정은 같은 사실에 대해 같은 결과를 내는 결정적 함수로 유지합니다. 유효한 도메인 입력은 항상 세 결과 중 하나를 반환하지만, 생성자가 막아야 할 내부 상태나 참조 무결성 위반은 `unknown`이 아니라 MCP 도구 오류입니다.

### 인정하는 근거

| 티어 | 근거                                                                                        | 역할                         |
| ---- | ------------------------------------------------------------------------------------------- | ---------------------------- |
| A    | 설치·관계 게이트: `requires_python`, `requires_dist`, npm `dependencies`·`peerDependencies` | 지원·비지원 판정 가능        |
| B    | npm `engines.node`, 사람이 검토해 저장소에 커밋한 공식 지원 정책과 릴리스 노트              | 지원·비지원 판정 가능        |
| C    | PyPI classifier 등 열거형 긍정 신호                                                         | 다른 근거의 지원 판정만 보강 |

중요한 판정 원칙은 다음과 같습니다.

- **티어 A 설치·관계 게이트 위반은 항상 `unsupported`입니다.** 명시적 호환성 진술이 반대라면 그 근거도 출처 카탈로그에 보존하고 불일치를 `notices`로 보고하지만, `verdict_evidence_ids`는 게이트 근거만 가리킵니다.
- npm `engines.node`는 기본적으로 경고이고 `engine-strict`일 때만 설치를 중단하므로 설치 게이트로 취급하지 않습니다. 게시자의 명시적 호환성 진술로 판정합니다.
- 공식 근거가 특정 버전을 명시적으로 포함하거나 배제하면 그 방향으로 판정합니다.
- **상한이 없는 제약이 나중에 출시된 런타임까지 지원한다고 확대 해석하지 않습니다.** 같은 회의를 하한에도 적용해, 선언 시점에 이미 지원 종료된 런타임에 대해서도 보강 근거를 요구합니다.
- 명시적 호환성 진술끼리 충돌하면 한쪽을 임의로 고르지 않고 `unknown / conflicting_evidence`를 반환합니다.
- 근거 부재, 부분 근거, 조회 실패, 선언된 관계 없음, 미지원 관계는 실패를 숨기지 않고 각각의 `unknown` 사유로 반환합니다.
- PyPI의 PEP 440·508은 `packaging`, npm SemVer는 `node-semver`의 strict mode로 각각 해석합니다. 서로 다른 버전 문법을 다른 문법으로 변환하지 않습니다.

## 오류와 `unknown`의 차이

입력 계약을 위반했거나 서버 불변식이 깨진 경우는 MCP 도구 오류입니다. 요청은 유효하지만 판정 근거가 부족한 경우는 정상적인 `unknown` 결과입니다.

| 구분      | 예시                                                                                       |
| --------- | ------------------------------------------------------------------------------------------ |
| 도구 오류 | 필수 필드 누락, 알 수 없는 필드, 등록되지 않은 namespace, 범위 버전 입력, 내부 불변식 위반 |
| `unknown` | 릴리스 미발견, 근거 부족·충돌, 외부 조회 실패, 선언된 관계 없음, 미지원 관계               |

`unknown`은 약한 성공이나 약한 실패가 아니라 "현재 근거로는 증명할 수 없음"을 나타내는 독립된 도메인 결과입니다. `limitations`와 `sources_checked`가 함께 오므로 다음 확인 지점을 알 수 있습니다.

---

## 제한사항

이 서버가 **하지 않는 일**과 **잘 못하는 일**을 명시합니다. 설계 문서가 인정한 한계를 여기에 모았습니다.

### 1. 큐레이션 팩이 비어 있는 상태로 배포됩니다

로더, 스키마 검증, 조회, 출력 경로는 모두 구현되고 실제 항목 fixture로 테스트되지만, 저장소에 커밋된 팩(`src/dependency_compat_mcp/curated/pack/compatibility.json`)의 `entries`는 **비어 있습니다**. 사람이 공식 출처와 대조해 리뷰한 근거만 팩에 들어갈 수 있고, 구현 과정에서 근거를 지어내지 않았기 때문입니다.

따라서 지금 이 서버의 모든 응답은 `depth: registry_only`이고 `limitations`에 `curated_pack_missing`이 붙습니다. 아래 2번 항목이 그 결과입니다.

### 2. 실질 가치는 큐레이션 팩 커버리지와 같습니다

이 서버만 제공할 수 있는 것은 **공식적으로 검증된 지원 선언**과 **`changes`(breaking change, removal, deprecation, migration)** 이며, 둘 다 사람이 검토해 저장소에 커밋한 큐레이션 팩에서만 나옵니다. 팩에 항목이 없는 대상에 대해 이 서버는 레지스트리 메타데이터를 구조화해 재전달할 뿐입니다.

커버리지를 넓히는 유일한 방법은 팩에 항목을 추가하는 것이고, 그것은 사람의 리뷰 노동입니다. 이는 설계상의 선택입니다 — 요청 경로에서 LLM을 배제한 대가로 커버리지를 잃고 근거의 검증 가능성을 얻었습니다.

응답의 `depth`가 `registry_only`이면 그 호출에서는 이 가치가 제공되지 않았다는 뜻입니다.

### 3. 패키지 간 판정은 의존성 resolver보다 정확하지 않습니다

이 서버는 subject의 `requires_dist`, `dependencies`, `peerDependencies`에 선언된 직접 제약만 봅니다. **전이 의존성, 버전 해석, 충돌 해소는 하지 않습니다.** 프로젝트 전체가 설치 가능한지 알고 싶다면 해당 생태계의 resolver를 사용하는 편이 정확합니다.

이 서버가 그 위에 더하는 것은 "선언된 제약이 무엇이고 그 근거가 어디에 있는가"입니다. 해결이 아니라 설명입니다.

### 4. `unknown`이 자주 나옵니다

이것은 결함이 아니라 설계 의도지만, 기대치를 맞춰 두는 편이 낫습니다. 다음 경우에 `unknown`이 나옵니다.

- `requires_python`에 상한이 없고, 물어본 런타임이 그 패키지 릴리스보다 나중에 나왔으며, 큐레이션 근거가 없을 때 (`open_upper_bound`)
- `requires_python`에 상한이 없고, 물어본 런타임이 그 패키지 릴리스 시점에 이미 지원 종료였을 때 (`stale_lower_bound`)
- classifier 같은 긍정 신호만 있고 설치·관계 게이트나 명시적 진술이 없을 때 (`tier_c_only`)
- 환경 marker나 extra로 가드된 조건부 의존성이라 환경 없이는 결론이 갈릴 때

"최신 Python + 임의의 패키지" 조합은 상당 비율이 `unknown`이 될 것으로 예상합니다. 이때 응답은 "설치는 되지만 저자가 검증했다는 근거는 없다"를 출처 카탈로그인 `evidence`와 미확인 범위인 `limitations`로 구분해 전달합니다. **호환된다고 추측하는 것보다 정직하지만, 확답을 원하는 사용자에게는 답답할 수 있습니다.**

### 5. 코드베이스를 보지 않습니다

서버는 저장소 경로, 소스 코드, manifest, lockfile을 입력받지 않습니다. 따라서 "당신의 프로젝트는 호환됩니다"라고 말할 수 없고 말하지 않습니다. 그 종합은 MCP 클라이언트의 책임입니다.

환경 marker(`python_version < "3.11"` 같은 조건)의 진리값도 같은 이유로 알 수 없습니다. 임의로 참으로 가정하지 않고 `marker_guarded_claim`으로 보고합니다.

### 6. 외부 자료가 사실인지, 최신인지 증명할 수 없습니다

타입과 구조는 응답의 형태를 보장하지만, 레지스트리 메타데이터가 정확한지, 공식 문서가 갱신되었는지, 큐레이션 항목이 여전히 유효한지는 증명하지 못합니다. 서버가 할 수 있는 것은 판단 재료를 남기는 것까지입니다: `provenance.reviewed_at`, `pack_version`, `verified_against`, `sources_checked`.

큐레이션 근거 URL의 생존도 보장되지 않습니다. 예약 CI가 링크 부패를 **검출**하지만 **예방**하지는 못합니다.

### 7. 지원 경계 밖

| 항목                                       | 상태                                       |
| ------------------------------------------ | ------------------------------------------ |
| `maven`, 임의 `product` namespace          | 등록하지 않음. 요구 미확인                 |
| 코드베이스 분석, 설치·업그레이드·코드 수정 | 범위 밖                                    |
| 전이 의존성 해석, 버전 해결                | 범위 밖 (`uv`/`pip` 사용)                  |
| 버전 범위 입력 (`>=3.10,<3.14`, `^19`)     | 입력 계약이 거부. 정확한 버전 하나만       |
| 대안 버전 추천 ("3.12를 쓰세요")           | 하지 않음. 서버는 추측하지 않음            |
| AI 기반 큐레이션 저작 파이프라인           | 저장소 구현 범위 밖. 외부 도구 사용만 허용 |

### 8. 요청 경로에 AI가 없습니다

이 항목은 제한사항이자 보장입니다. 서버는 요청 처리 중 LLM이나 다른 MCP 서버를 호출하지 않습니다. 따라서 근거를 생성하거나 문서를 요약하거나 검색 결과를 종합하지 않습니다. 커버리지가 좁은 대신 같은 입력과 같은 외부 사실은 같은 정규화 결과를 내며, 이 성질은 `retrieved_at`을 제외한 결정성 테스트로 검증됩니다.

큐레이션 팩에는 사람이 공식 출처와 대조하고 리뷰한 정적 데이터만 커밋하며, 런타임은 이 데이터만 읽습니다.

---

## 구현 계약

`docs/01`~`04`가 정의한 다음 범위를 제공합니다.

| 범위                                                                     | 위치                                            |
| ------------------------------------------------------------------------ | ----------------------------------------------- |
| `check_compatibility`, `get_compatibility_context`과 02·04의 입출력 계약 | `server.py`, `contracts/`                       |
| 네 관계 규칙과 규칙별 방향 정책                                          | `domain/relations.py`                           |
| 단계 0~7 결정 절차 (순수 총함수)                                         | `domain/evaluate.py`                            |
| PyPI JSON API, npm registry, 런타임 릴리스·EOL 표, 큐레이션 팩, TTL 캐시 | `adapters/`, `curated/`, `infra/cache.py`       |
| `packaging`/`node-semver`를 분리한 PEP 440·508 및 npm SemVer 처리        | `domain/versions.py`, `domain/targets.py`       |
| 로컬 `stdio`와 원격 무상태 Streamable HTTP                               | `cli.py`                                        |
| 시간·응답 크기·허용 호스트 제한, 취소 전파                               | `infra/http.py`                                 |
| 근거 참조 무결성 검증                                                    | `contracts/assembly.py`, `contracts/outputs.py` |

종속성은 `mcp`, `packaging`, `node-semver` 세 개이며 `uv.lock`과 동기화되어 있습니다. 그 밖의 종속성은 추가·승격·교체하지 않았습니다.

### 검증

```bash
uv run pytest            # 608 tests
uv tool run ruff check .
uv tool run pyrefly check
```

문서가 요구한 검증 기준을 다음 테스트가 담당합니다.

| 기준                                          | 테스트                                                                          |
| --------------------------------------------- | ------------------------------------------------------------------------------- |
| 인메모리 클라이언트로 스키마·구조화 출력      | `tests/test_mcp_contract.py`                                                    |
| `stdio`와 `/mcp`에서 같은 도구·결과           | `tests/test_transports.py`                                                      |
| 판정 총함수 성질, 티어 규칙 불변식            | `tests/test_evaluate.py`                                                        |
| 파서 round-trip, 경계 버전, marker·prerelease | `tests/test_targets.py`, `tests/test_versions.py`, `tests/test_markers.py`      |
| 어댑터 계약(비정상 JSON, 크기, rate limit)    | `tests/test_pypi_adapter.py`, `tests/test_npm_adapter.py`, `tests/test_http.py` |
| 결정성(`retrieved_at` 제외 동일 바이트)       | `tests/test_determinism.py`                                                     |
| 요청 경로에 모델 호출 없음(계층 규칙)         | `tests/test_layering.py`                                                        |
