with import <nixpkgs> {};

python3.pkgs.buildPythonApplication rec {
  name = "nix-review";
  src = ./.;
  env = buildEnv { inherit name; paths = buildInputs ++ checkInputs; };
  buildInputs = [ makeWrapper ];
  checkInputs = [ mypy ];
  checkPhase = ''
    ${python3.interpreter} -m unittest discover .
    mypy nix_review
  '';
  makeWrapperArgs = [
    "--prefix PATH" ":" "${nix}/bin"
  ];
}
