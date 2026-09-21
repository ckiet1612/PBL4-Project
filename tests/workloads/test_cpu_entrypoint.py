from nexa.workloads.cpu_entrypoint import run_cpu_file


def test_cpu_entrypoint_writes_only_result_file(tmp_path) -> None:
    input_path = tmp_path / "input.json"
    output_path = tmp_path / "out" / "result.json"
    input_path.write_bytes(b'{"initial_value":2}')
    result = run_cpu_file(
        input_path=input_path,
        output_path=output_path,
        iterations=2,
        seed=1,
        modulus=97,
        spec_checksum="sha256:" + "f" * 64,
    )
    assert output_path.read_bytes() == result.result_bytes
