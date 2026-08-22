# 큐레이션 근거 팩

큐레이션 근거 팩은 사람이 공식 문서를 직접 읽고 확인한 호환성 정보를 JSON으로 기록한 자료입니다. PyPI와 npm의 레지스트리 메타데이터는 패키지 게시자가 선언한 설치 조건을 보여 주지만, 게시자가 특정 버전 조합을 공식 지원하는지까지 항상 알려 주지는 않습니다. 큐레이션 근거 팩은 공식 지원 범위를 `statements`에 기록하고, 호환성을 깨뜨릴 수 있는 변경 사항을 `changes`에 기록하여 레지스트리 메타데이터를 보완합니다. 서버가 공식 문서를 근거로 답할 수 있는 질문의 범위는 큐레이션 근거 팩에 기록된 항목의 범위와 같습니다.

배포되는 큐레이션 근거 팩은 **의도적으로 비어 있습니다.** `compatibility.json`의 `entries: []`는 검토된 항목이 아직 없다는 사실을 정확하게 나타냅니다. 문서에 있는 예시를 실제 근거로 복사하거나 파일이 채워진 것처럼 보이게 하려고 근거를 만들어서는 안 됩니다. 로더, 스키마 검증, 항목 조회, 응답 변환은 `tests/fixtures/packs/`의 테스트용 팩으로 검증합니다.

## 파일 구성

| 파일                                                   | 역할                                                                                   |
| ------------------------------------------------------ | -------------------------------------------------------------------------------------- |
| `compatibility.json`                                   | 공식 문서에서 검토한 호환성 근거를 담는 팩입니다. 현재 `entries` 목록은 비어 있습니다. |
| [`../runtime_releases.json`](../runtime_releases.json) | CPython과 Node.js의 릴리스 날짜 및 지원 종료일을 담는 별도의 생성 자료입니다.          |
| [`../loader.py`](../loader.py)                         | 이 문서에 설명된 입력 규칙을 서버 시작 시 검사하는 코드입니다.                         |

서버는 `src/dependency_compat_mcp/curated/pack/` 디렉터리에 있는 모든 `*.json` 파일을 하나의 큐레이션 근거 팩으로 읽습니다. 모든 JSON 파일은 같은 근거 자료 상태를 나타내기 위해 동일한 `pack_version`을 선언해야 합니다. 서버는 각 큐레이션 근거의 출처 정보인 `provenance`에 `pack_version`을 포함합니다. 이전 조회 결과를 보관하는 캐시의 키에도 `pack_version`이 포함되므로, 팩 버전이 바뀌면 이전 팩으로 만든 결과를 재사용하지 않습니다.

## 사람이 검토했다는 의미

큐레이션 항목은 **사람이 공식 출처를 읽고 항목의 내용과 일치한다고 확인했다는 기록**입니다. 큐레이션 검토 규칙은 추측과 검토된 근거를 구분하기 위해 적용합니다.

1. **기억에 의존하지 말고 출처를 직접 읽어야 합니다.** 검토자는 `source.url`을 열고 공식 문서의 내용이 큐레이션 항목과 일치하는지 확인해야 합니다. 외부 AI 도구가 초안을 만들 수는 있지만, 사람이 공식 출처와 대조하기 전의 초안은 근거가 아닙니다.
2. **`reviewed_by`와 `reviewed_at`에는 실제 검토자와 검토 날짜를 기록해야 합니다.** `reviewed_by`에는 공식 출처와 항목을 대조한 사람을 기록하고, `reviewed_at`에는 대조를 마친 날짜를 기록합니다. 두 필드는 항목의 검토 책임자와 검토 시점을 확인하는 데 필요합니다. 자동 검사(CI)는 `reviewed_at`이 설정된 검토 주기보다 오래되면 경고합니다.
3. **지원 진술과 변경 사항에는 각각 출처가 하나씩 필요합니다.** 모든 `statements[]` 항목과 `changes[]` 항목에는 `source.url`이 있어야 합니다. 출처 URL이 하나라도 빠지면 로더가 팩 전체를 거부하므로 서버가 시작되지 않습니다.
4. **출처 URL은 프로젝트 게시자가 관리하는 공식 사이트여야 합니다.** 로더는 [`loader.py`](../loader.py)의 `OFFICIAL_HOSTS` 목록에 있는 호스트만 허용합니다. 블로그, 포럼, 질의응답 사이트, 요약 사이트는 공식 근거로 사용할 수 없습니다. 새 호스트를 `OFFICIAL_HOSTS`에 추가하는 변경은 서버 전체가 신뢰하는 출처 범위를 넓히므로 별도 검토가 필요합니다. 새 호스트를 추가하는 풀 리퀘스트(PR)에는 해당 호스트가 프로젝트 게시자의 공식 사이트인 이유를 적어야 합니다.
5. **변경 사항의 `category`는 영향보다 약하게 기록해서는 안 됩니다.** 허용 값은 호환성이 깨지는 변경인 `breaking_change`, 기능 삭제인 `removal`, 지원 중단 예고인 `deprecation`, 사용자가 마이그레이션 작업을 해야 한다는 뜻의 `migration_required` 네 가지입니다. 검토자는 공식 문서가 설명하는 실제 영향에 맞는 값을 선택해야 합니다.
6. **접속할 수 없는 출처 URL이 생기면 항목을 정비해야 합니다.** 예약 CI는 페이지에 접속할 수 있는지만 확인하는 HTTP HEAD 요청을 모든 `source.url`에 보내고 실패를 보고합니다. 출처 URL 접속 실패만으로 빌드가 중단되지는 않습니다. 사용자가 확인할 수 없는 URL은 근거 역할을 하지 못하므로, 공식 문서의 새 URL로 바꾸거나 해당 항목을 제거해야 합니다.

## 항목 형식

다음 JSON은 필드 형식을 보여 주기 위한 가상 예시입니다. `example-framework`와 `.invalid` 도메인은 실제 근거가 아니므로 큐레이션 팩에 그대로 추가할 수 없습니다.

```json
{
  "namespace": "pypi",
  "name": "example-framework",
  "applies_to": ">=5.2,<5.3",
  "verified_against": ["5.2", "5.2.1"],
  "reviewed_at": "2026-08-10",
  "reviewed_by": "REVIEWER_HANDLE",
  "statements": [
    {
      "stance": "supports",
      "counterpart": { "namespace": "runtime", "name": "python" },
      "expression": ">=3.10,<3.14",
      "scheme": "pep440",
      "source": {
        "source_type": "official_support_policy",
        "title": "Supported Python versions",
        "url": "https://docs.example.invalid/supported-versions"
      }
    }
  ],
  "changes": [
    {
      "category": "removal",
      "area": "removed_api",
      "summary": "The API was removed in this release.",
      "source": {
        "source_type": "official_release_note",
        "title": "Version 5.2 release notes",
        "url": "https://docs.example.invalid/releases/5.2"
      }
    }
  ]
}
```

| 필드               | 서버가 시작할 때 검사하는 규칙                                                                                                                    |
| ------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------- |
| `namespace`        | 대상 종류를 나타내며 `pypi`, `npm`, `runtime` 중 하나여야 합니다.                                                                                 |
| `name`             | 패키지 또는 런타임 이름입니다. 도구 입력과 동일한 이름 문법을 사용하며, `runtime`에서는 `python` 또는 `node`만 허용합니다.                        |
| `applies_to`       | 이 항목이 적용되는 릴리스 범위입니다. PyPI와 Python에는 PEP 440을 사용하고, npm과 Node.js에는 SemVer를 사용합니다.                                |
| `verified_against` | 검토자가 공식 문서와 직접 대조한 정확한 버전의 목록입니다. `v` 같은 접두사를 붙이지 않은 정식 버전 표기만 허용하며, 목록은 비어 있을 수 있습니다. |
| `reviewed_at`      | 공식 출처와 항목을 대조한 날짜이며 `YYYY-MM-DD` 형식의 필수 값입니다.                                                                             |
| `reviewed_by`      | 공식 출처와 항목을 대조한 사람을 나타내는 비어 있지 않은 필수 값입니다.                                                                           |
| `statements`       | 게시자가 밝힌 공식 지원 또는 비지원 범위의 목록입니다.                                                                                            |
| `stance`           | `supports`는 게시자가 지원한다고 밝힌 범위를, `excludes`는 게시자가 지원하지 않는다고 밝힌 범위를 뜻합니다.                                       |
| `counterpart`      | `statements`의 지원 또는 비지원 범위가 가리키는 상대 패키지나 런타임입니다.                                                                       |
| `expression`       | `counterpart`에 적용되는 버전 범위입니다.                                                                                                         |
| `scheme`           | `expression`의 버전 문법이며 `pep440` 또는 `semver`여야 합니다. 선택한 문법은 `counterpart`가 속한 생태계와 일치해야 합니다.                      |
| `changes`          | 호환성을 깨뜨릴 수 있는 변경, 기능 삭제, 지원 중단 예고, 필요한 마이그레이션 작업의 목록입니다.                                                   |
| `category`         | `breaking_change`, `removal`, `deprecation`, `migration_required` 중 하나여야 합니다.                                                             |
| `area`             | 변경된 기능이나 API 영역을 식별하는 짧은 코드입니다.                                                                                              |
| `summary`          | 공식 문서에 적힌 변경 내용을 요약한 문장입니다.                                                                                                   |
| `source_type`      | 공식 지원 정책은 `official_support_policy`, 공식 릴리스 노트는 `official_release_note`를 사용합니다.                                              |
| `source.title`     | 공식 출처 페이지의 제목입니다.                                                                                                                    |
| `source.url`       | 각 지원 진술 또는 변경 사항을 직접 확인할 수 있는 공식 출처 URL입니다.                                                                            |

스키마에 정의되지 않은 필드는 허용하지 않습니다. 전체 디렉터리에서 `namespace`, `name`, `applies_to`가 모두 같은 항목은 하나만 존재해야 합니다. 세 필드의 조합을 고유하게 유지하는 규칙은 같은 질문에 여러 항목이 겹칠 때 JSON 파일을 읽은 순서에 따라 결과가 달라지는 문제를 막습니다.

### `applies_to`와 `verified_against`의 차이

`applies_to`는 공식 문서의 진술이 적용된다고 기록한 전체 릴리스 범위입니다. `verified_against`는 검토자가 공식 문서를 보면서 직접 확인한 정확한 버전만 나열합니다.

예시 항목의 `applies_to`는 `>=5.2,<5.3`이므로 5.2 계열 전체에 적용됩니다. 예시 항목의 `verified_against`에는 `5.2`와 `5.2.1`만 있으므로, 검토자가 직접 대조한 버전은 두 개뿐입니다. 서버는 5.2.2처럼 `applies_to`에는 포함되지만 `verified_against`에는 없는 버전에도 해당 진술을 사용합니다. 서버는 직접 대조하지 않은 버전에 진술을 사용할 때 `curated_not_verified_for_version` 제한 코드를 응답에 추가하여 검토 범위를 알립니다.

## 항목 추가 방법

1. 공식 문서를 직접 열고 지원 진술이나 변경 사항이 적용되는 정확한 릴리스 범위를 확인합니다.
2. 공식 문서가 다루는 전체 릴리스 범위를 `applies_to`에 기록합니다.
3. 검토자가 공식 문서와 직접 대조한 정확한 버전만 `verified_against`에 기록합니다.
4. 공식 지원 또는 비지원 진술마다 `statements[]` 항목을 하나씩 추가합니다.
5. 호환성을 깨뜨리는 변경, 기능 삭제, 지원 중단 예고, 필요한 마이그레이션 작업마다 `changes[]` 항목을 하나씩 추가합니다.
6. 각 `source.title`에는 공식 출처 페이지에 표시된 제목을 기록하고, 각 `source.url`에는 내용을 직접 확인할 수 있는 공식 URL을 기록합니다.
7. `uv run pytest tests/test_curated_loader.py`를 실행합니다. 큐레이션 팩의 스키마 오류는 서버 시작을 막으므로 로더 테스트가 반드시 통과해야 합니다.
8. 풀 리퀘스트(PR) 설명에 모든 공식 출처 URL을 적습니다. PR 검토자는 같은 페이지를 열어 큐레이션 항목의 내용을 다시 확인해야 합니다.

## 런타임 릴리스 표

[`../runtime_releases.json`](../runtime_releases.json)은 공식 자료에서 생성한 CPython과 Node.js 릴리스 목록입니다. `runtime_releases.json`은 큐레이션 근거 팩의 예시나 테스트용 가짜 자료(fixture)가 아니라 서버가 실제로 사용하는 완전한 자료입니다. 현재 파일의 `generated_at` 값은 `2026-08-12`이며, 이 날짜는 자료를 생성한 시점을 나타냅니다.

런타임 릴리스 표는 다음 명령으로 다시 생성합니다.

```sh
uv run scripts/build_runtime_releases.py
```

생성기는 python.org 다운로드 API, peps.python.org 릴리스 주기 자료, nodejs.org 배포 목록, nodejs/Release 저장소의 `schedule.json`이라는 네 공식 출처를 읽습니다. 생성기는 검토하기 쉬운 변경 내역을 만들기 위해 키를 일정한 순서로 정렬하고 JSON 들여쓰기를 두 칸으로 고정합니다. 생성 명령을 실행한 뒤에는 생성된 diff를 검토해야 하며, `runtime_releases.json`을 손으로 수정해서는 안 됩니다.

`eol_at`은 개별 패치 버전이 아니라 해당 릴리스 계열 전체의 지원 종료일입니다. 예를 들어 모든 Python 3.13.x 릴리스는 같은 `eol_at`을 사용하고, 모든 Node.js 22.x 릴리스도 같은 `eol_at`을 사용합니다. 공식 출처가 `YYYY-MM`까지만 공개하여 정확한 날짜를 알 수 없으면 `eol_at`은 `null`입니다. 월 정보에 임의의 날짜를 붙이면 공식 출처에 없는 사실을 만들게 되므로 생성기는 날짜를 추정하지 않습니다. 서버는 비교 대상 런타임이 패키지 출시 전에 이미 지원 종료됐는지 확인할 때만 `eol_at`을 사용합니다. `eol_at`이 `null`이거나 미래 날짜이면 서버는 런타임 지원 종료 여부를 근거로 판정을 제한하지 않습니다.
