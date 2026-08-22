# dependency-compat-mcp

**"이 두 버전, 같이 써도 됩니까?"**에 공식 1차 출처와 함께 답하는 Model Context Protocol(MCP) 서버입니다.

서버는 정확한 두 릴리스, 즉 배포된 두 버전(예: `pypi:django@5.2`와 `runtime:python@3.13`)을 비교합니다. 서버는 비교 결과를 `supported` / `unsupported` / `unknown` 중 하나로 표시하고, **판단에 사용한 근거도 함께** 돌려줍니다. 서버는 요청을 처리하는 동안 언어 모델을 호출하지 않습니다. 버전 범위 해석과 비교는 Python 패키지용 `packaging`과 npm 패키지용 `node-semver`가 담당합니다.

공개 서버를 Claude Code에 등록하려면 다음 명령을 실행합니다.

```bash
claude mcp add --transport http dependency-compat \
  https://dependency-compat-mcp-git-769945419767.asia-northeast3.run.app/mcp
```

**쓰기 전에 알아둘 것 세 가지**

- 이 서버가 판정할 수 있는 버전 관계는 [네 종류](#무엇을-물어볼-수-있는가)뿐입니다. 지원하지 않는 관계를 입력하면 외부 자료를 조회하지 않고 `unknown`을 돌려줍니다.
- 이 서버는 직접 연결된 두 대상만 비교하며, 그 대상이 다시 요구하는 하위 의존성까지 따라가지는 않습니다. 프로젝트 전체가 설치되는지는 `uv lock`이나 `npm install` 같은 의존성 해결 명령으로 확인해야 합니다.
- 서버는 호환성 정보를 저장소에 보관하지 않습니다. 판정에 필요한 자료는 요청을 받을 때마다 PyPI, npm, python.org, nodejs.org 같은 공식 출처에서 직접 조회합니다. 그래서 저장소를 갱신하지 않아도 새로 나온 Python·Node.js 릴리스를 바로 인식합니다.

---

## 설치

**요구 사항** — 서버에 연결하는 MCP 클라이언트는 MCP `2026-07-28` 개정판을 지원해야 합니다. 이 서버는 MCP `2026-07-28`만 구현하며, 이전 개정판으로 자동 전환하지 않습니다. 서버를 직접 실행하려면 Python 3.14 이상이 필요합니다. 직접 실행한 서버는 아래 여섯 개 주소에 HTTPS 요청을 보낼 수 있어야 합니다. 서버는 이 목록에 없는 주소로는 요청을 보내지 않습니다.

| 조회 대상                    | 주소                                                  |
| ---------------------------- | ----------------------------------------------------- |
| PyPI 패키지 메타데이터       | `pypi.org`                                            |
| npm 패키지 메타데이터        | `registry.npmjs.org`                                  |
| Python 릴리스 목록과 출시일  | `www.python.org`                                      |
| Python 릴리스별 지원 종료일  | `peps.python.org`                                     |
| Node.js 릴리스 목록과 출시일 | `nodejs.org`                                          |
| Node.js 릴리스별 지원 종료일 | `raw.githubusercontent.com` (`nodejs/Release` 저장소) |

### 바로 연결할 수 있는 공개 서버 (권장)

```bash
# Claude Code
claude mcp add --transport http dependency-compat \
  https://dependency-compat-mcp-git-769945419767.asia-northeast3.run.app/mcp

# Codex CLI
codex mcp add dependency-compat \
  --url https://dependency-compat-mcp-git-769945419767.asia-northeast3.run.app/mcp
```

MCP 클라이언트 설정 파일에 직접 등록하려면 다음 값을 사용합니다.

```json
{
  "mcpServers": {
    "dependency-compat": {
      "type": "http",
      "url": "https://dependency-compat-mcp-git-769945419767.asia-northeast3.run.app/mcp"
    }
  }
}
```

### 표준 입출력(stdio)으로 로컬 실행

Claude Code가 로컬 서버 프로세스를 실행하도록 등록하려면 다음 명령을 사용합니다.

```bash
claude mcp add dependency-compat -- uvx --python 3.14 dependency-compat-mcp
```

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

### HTTP 서버 직접 호스팅

```bash
uv run dependency-compat-mcp --transport http --host 127.0.0.1 --port 8000
```

HTTP 전송은 `POST /mcp` 엔드포인트 하나를 사용합니다. HTTP 서버는 클라이언트별 세션 상태를 저장하지 않으므로, 같은 설정의 서버 인스턴스를 여러 개 실행할 수 있습니다. 서버는 `--host`와 `--port`에 지정한 주소를 기준으로 허용할 HTTP `Host` 및 `Origin` 헤더를 정합니다. 클라이언트와 서버 사이에서 요청을 전달하는 리버스 프록시가 요청 주소를 다른 `Host` 헤더로 보내면 DNS 재바인딩 공격 방어가 해당 요청을 거부합니다. 인터넷에 서버를 공개할 때는 리버스 프록시나 클라우드 서비스에서 HTTPS 암호화(TLS)와 사용자 인증을 구성해야 합니다.

---

## 무엇을 물어볼 수 있는가

비교 대상 하나는 `namespace`, `name`, `version`이라는 세 필드로 식별합니다. `namespace`는 대상의 종류를 나타내며 `pypi`, `npm`, `runtime` 중 하나여야 합니다. `name`은 패키지 이름이며, `runtime` 대상에서는 `python` 또는 `node`여야 합니다. `version`에는 버전 범위가 아닌 정확한 버전 하나를 입력해야 합니다. PyPI와 Python 버전은 PEP 440 문법으로, npm과 Node.js 버전은 엄격한 SemVer 문법으로 해석합니다.

판정할 수 있는 관계는 넷입니다. 이 표에 없는 조합은 **외부 조회를 시작하기 전에** 바로 `unknown`으로 끝납니다.

| 비교 대상                 | 서버가 확인하는 선언                                                           | 입력 순서                          |
| ------------------------- | ------------------------------------------------------------------------------ | ---------------------------------- |
| `pypi` × `runtime:python` | PyPI 패키지가 지원하는 Python 범위인 `requires_python`                         | 두 대상을 바꿔 입력해도 같은 질문  |
| `npm` × `runtime:node`    | npm 패키지가 지원한다고 밝힌 Node.js 범위인 `engines.node`                     | 두 대상을 바꿔 입력해도 같은 질문  |
| `pypi` × `pypi`           | 첫 번째 패키지(`subject`)가 요구하는 두 번째 패키지 범위인 `requires_dist`     | **입력 순서에 따라 질문이 달라짐** |
| `npm` × `npm`             | 첫 번째 패키지(`subject`)의 `dependencies` 또는 `peerDependencies`에 적힌 범위 | **입력 순서에 따라 질문이 달라짐** |

패키지끼리는 `A → B`와 `B → A`가 서로 다른 질문이므로, 서버가 임의로 순서를 바꾸지 않습니다.

**받지 않는 입력** — 모두 도구 오류로 돌려보내며, 알아서 고쳐 주지 않습니다.

- 버전 범위(`>=3.10,<3.14`, `^19`), 접두사 붙은 버전(`v22.17.0`)
- 저장소 경로, 소스 코드, 패키지 설정 파일(manifest), 설치 버전을 고정한 파일(lockfile), 임의 URL
- 지원 대상 종류로 등록되지 않은 `namespace`(`maven`, `runtime:ruby` 등)

---

## 무엇을 돌려주는가

- **`check_compatibility`** — 릴리스 둘을 받아, 한쪽이 선언한 버전 제약을 다른 쪽이 만족하는지 비교합니다. 응답에는 판정, 판정 이유, 근거 URL이 들어 있습니다.
- **`get_compatibility_context`** — 릴리스 하나를 받아, 해당 릴리스가 선언한 버전 제약을 돌려줍니다. 이 도구는 다른 릴리스와 비교하거나 호환 여부를 판정하지 않습니다.

다음 JSON은 `check_compatibility`에 Django 5.2와 asgiref 3.8.1을 비교하도록 요청하는 예입니다.

```json
{
  "subject": { "namespace": "pypi", "name": "django", "version": "5.2" },
  "counterpart": { "namespace": "pypi", "name": "asgiref", "version": "3.8.1" }
}
```

다음 JSON은 설명에 필요한 필드만 남긴 응답 예입니다.

```json
{
  "verdict": "supported",
  "summary": "pypi:asgiref 3.8.1 satisfies pypi:django 5.2's requires_dist, backed by 1 source(s).",
  "evidence": [
    {
      "id": "evidence-1",
      "url": "https://pypi.org/project/django/5.2/",
      "substantiates": "django 5.2 declares Requires-Dist: asgiref>=3.8.1.",
      "expression": ">=3.8.1"
    }
  ],
  "verdict_evidence_ids": ["evidence-1"]
}
```

| 결과          | 의미                                           |
| ------------- | ---------------------------------------------- |
| `supported`   | 정확한 버전 조합을 지지하는 명시적 근거가 있음 |
| `unsupported` | 정확한 버전 조합을 배제하는 명시적 근거가 있음 |
| `unknown`     | 현재 근거로 어느 쪽도 증명할 수 없음           |

서버의 응답 자료형은 `supported`와 `unsupported` 판정에 직접 근거가 **적어도 하나** 포함되도록 강제합니다. `verdict_evidence_ids`에는 `evidence` 목록 중 판정을 직접 뒷받침하는 항목의 `id`가 들어갑니다. `unknown`은 도구 실행 실패가 아니라 현재 근거만으로 결론을 낼 수 없다는 정상적인 판정입니다. `unknown` 응답에는 결론을 막은 원인과 추가로 확인할 자료가 정해진 코드 값으로 들어갑니다.

MCP 클라이언트는 `tools/list` 응답에 포함된 JSON 스키마에서 전체 응답 필드와 허용 값을 확인할 수 있습니다.

---

## 판정 규칙

서버는 근거가 판정에 미치는 영향에 따라 근거를 A, B, C 세 등급으로 나눕니다.

| 등급  | 포함되는 근거                                                                                       | 판정에서 하는 역할                                       |
| ----- | --------------------------------------------------------------------------------------------------- | -------------------------------------------------------- |
| **A** | 설치에 필요한 버전 조건인 `requires_python`, `requires_dist`, npm `dependencies`·`peerDependencies` | 조건을 만족하면 지원, 위반하면 비지원으로 판정할 수 있음 |
| **B** | npm 게시자가 적은 `engines.node`                                                                    | 게시자가 밝힌 지원 또는 비지원 범위로 사용               |
| **C** | PyPI 페이지에서 지원 Python 버전을 표시하는 `Programming Language :: Python` 분류 항목(classifier)  | 다른 근거를 보강할 수 있지만 단독 판정에는 사용하지 않음 |

- **A 등급의 필수 설치 조건을 위반하면 항상 `unsupported`입니다.** 게시자가 밝힌 지원 범위에 반대 내용이 있어도 필수 설치 조건을 우선하며, 서로 다른 내용이 발견됐다는 사실은 응답의 `notices` 필드에 남깁니다.
- npm의 `engines.node` 불일치는 npm 설정 `engine-strict`를 켠 경우에만 설치를 막습니다. 서버는 `engines.node`를 필수 설치 조건이 아니라 게시자가 밝힌 지원 범위로 취급합니다.
- **최대 버전을 적지 않은 제약을 "앞으로 나올 모든 릴리스를 지원한다"로 넓혀 읽지 않습니다.** 패키지가 배포될 때 이미 지원이 끝난 런타임도 설치 조건만으로 공식 지원한다고 판단하지 않고 추가 근거를 요구합니다.
- **지원 종료일을 확인하지 못한 채로 지원한다고 판단하지 않습니다.** 최대 버전이 없는 제약을 판정할 때는 상대 런타임이 이미 지원 종료됐는지 확인해야 합니다. 공식 지원 일정 자료를 읽지 못했다면 그 사실을 `decision_causes`의 `lifecycle_unavailable`로 알리고 `unknown`을 반환합니다. **공식 출처가 지원 종료일을 아직 발표하지 않은 경우와 조회에 실패한 경우는 서로 다르게 처리합니다.**
- **릴리스가 없다는 판정과 조회하지 못했다는 판정을 구분합니다.** 공식 릴리스 목록을 읽은 결과 해당 버전이 없으면 `release_not_found`이고, 목록 자체를 읽지 못하면 `lookup_failed`입니다. 조회 실패를 "그런 버전은 없다"로 바꾸어 답하지 않습니다.
- 공식 지원 범위를 설명하는 명시적 진술끼리 충돌하면 한쪽을 고르지 않고 `unknown`을 반환합니다.
- 서로 다른 패키지 생태계의 버전 문법을 **서로 바꾸어 해석하지 않습니다.** 두 값의 버전 체계가 다르면 비교하지 않습니다.

입력이 정해진 형식을 어긴 경우(필수 필드 누락, 정의되지 않은 필드, 등록되지 않은 `namespace`, 범위 버전)는 도구 오류를 반환합니다. 입력은 올바르지만 근거가 부족하거나 충돌하거나 외부 자료 조회가 실패한 경우에는 `unknown`을 반환합니다.

---

## 제한사항

**1. 판정 근거는 기계가 읽을 수 있는 공식 자료로 한정됩니다.** 서버는 패키지 레지스트리가 게시한 메타데이터와 Python·Node.js가 게시한 릴리스 목록 및 지원 일정만 사용합니다. 사람이 공식 문서를 읽고 정리해야만 알 수 있는 내용, 예를 들어 문서로만 밝힌 지원 정책, 호환성을 깨뜨리는 변경(`breaking_change`), 지원 중단 예고(`deprecation`)는 판정에 사용하지 않습니다. **요청 처리 과정에서 언어 모델을 사용하지 않기 때문에 답변 범위는 좁지만, 사용자는 모든 판단 근거를 직접 검증할 수 있습니다.**

**2. 외부 출처를 조회하지 못하면 판정하지 않습니다.** 서버는 저장소에 사본을 두지 않으므로, 공식 출처를 읽지 못하면 답을 만들어 내지 않고 `unknown`을 반환합니다. 응답의 `sources_checked` 항목은 어떤 출처를 어떤 대상으로 조회했고 결과가 무엇이었는지 한 줄씩 기록하며, 필수 조회와 보조 조회를 `required` 값으로 구분합니다. 릴리스 목록 조회는 필수이고, 지원 종료일 조회는 보조입니다. 보조 조회가 실패해도 요청 전체가 실패하지는 않지만, 그 사실은 `source_unavailable` 제한 사항으로 남고 그 조회에 의존하는 판정은 내리지 않습니다.

**3. 프로젝트 전체의 설치 가능성을 계산하는 의존성 해결 도구(resolver)를 대체하지 않습니다.** 서버는 첫 번째 입력인 `subject`가 두 번째 입력에 대해 선언한 **직접 버전 제약**만 확인합니다. 서버는 하위 의존성을 따라가거나, 버전 범위에서 설치할 버전을 고르거나, 여러 패키지의 버전 충돌을 해결하지 않습니다. **이 서버의 역할은 의존성 해결이 아니라 선언된 조건과 근거를 설명하는 것입니다.**

**4. 근거가 충분하지 않으면 `unknown`이 자주 나옵니다.** 버전 제약에 상한이 없거나, PyPI 분류 항목 같은 간접적인 긍정 신호만 있거나, 특정 운영체제·Python 버전 등에서만 적용되는 환경 조건(marker)이 붙어 있거나, 두 값의 버전 체계가 달라 비교할 수 없으면 `unknown`이 될 수 있습니다. 최신 Python과 임의의 패키지를 비교하는 질문도 공식 지원 근거가 없으면 `unknown`이 될 수 있습니다. `unknown` 응답은 **선언된 설치 조건을 만족한다는 사실과 해당 버전을 공식 지원한다고 확인할 근거가 없다는 사실을 구분해서** 전달합니다.

**5. 사용자의 코드베이스와 설치 환경을 검사하지 않습니다.** 서버는 저장소 경로나 설치 버전을 고정한 lockfile을 입력받지 않으므로 "사용자의 프로젝트 전체가 호환된다"고 판정할 수 없습니다. 서버는 운영체제나 Python 버전처럼 환경 조건(marker)의 참·거짓을 결정하는 값도 입력받지 않으므로 조건을 임의로 참이라고 가정하지 않습니다. 프로젝트와 실행 환경을 함께 고려한 최종 판단은 이 서버의 응답을 사용하는 MCP 클라이언트가 담당해야 합니다.

**6. 가져온 자료가 사실인지, 최신인지까지는 보증하지 못합니다.** 레지스트리 메타데이터가 정확한지, 공식 문서가 그사이 갱신되었는지는 서버가 알 수 없습니다. 서버는 무엇을 언제 조회했고 조회 결과가 어땠는지 기록하여 사용자가 판단 근거를 다시 확인할 수 있게 합니다.

**7. 서버는 분석 결과를 바탕으로 프로젝트를 변경하지 않습니다.** 서버는 코드베이스 분석, 패키지 설치·업그레이드, 코드 수정, 설치 버전 결정, 다른 버전 추천(예: "3.12를 사용하십시오")을 하지 않습니다. 서버는 요청을 처리하는 동안 언어 모델이나 다른 MCP 서버도 호출하지 않습니다.

---

## 기여

이슈와 Pull Request를 환영합니다. 판정 규칙과 응답 형식을 바꾸는 변경은 그 이유를 함께 설명해 주십시오. 조회 대상 주소 목록을 넓히는 변경은 서버가 사실로 받아들이는 자료의 범위를 넓히는 일이므로, 해당 주소가 그 정보의 공식 게시처인 이유를 함께 적어야 합니다.

```bash
git clone https://github.com/dongju93/dependency-compat-mcp.git
cd dependency-compat-mcp && uv sync && uv run pytest
```

## 라이선스

MIT 라이선스로 배포합니다. 전문은 [LICENSE](LICENSE)에 있습니다.
