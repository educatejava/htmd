# (c) 2015-2022 Acellera Ltd http://www.acellera.com
# All Rights Reserved
# Distributed under HTMD Software License Agreement
# No redistribution in whole or part
#
from moleculekit.tools.graphalignment import makeMolGraph, compareGraphs
from moleculekit.molecule import Molecule
from moleculekit.util import ensurelist
from htmd.home import home
from glob import glob
import unittest
import shutil
import parmed
import numpy as np
import tempfile
import os
import logging

logger = logging.getLogger(__name__)


ace = Molecule(os.path.join(home(shareDir=True), "builder", "caps", "ACE.cif"))
nme = Molecule(os.path.join(home(shareDir=True), "builder", "caps", "NME.cif"))


def _cap_residue(mol):
    mol = mol.copy()
    resn = mol.resname[0]

    acec = ace.copy()
    acec.align("name N H CA", refmol=mol)
    acec.remove("name N H CA", _logger=False)

    nmec = nme.copy()
    nmec.align("name CA C O", refmol=mol)
    nmec.remove("name CA C O", _logger=False)

    mol.insert(acec, index=0)
    mol.append(nmec)
    # Bond ACE/NME to the residue
    mol.bonds = np.vstack(
        (
            mol.bonds,
            [
                np.where((mol.resname == "NME") & (mol.name == "N"))[0][0],
                np.where((mol.resname == resn) & (mol.name == "C"))[0][0],
            ],
            [
                np.where((mol.resname == "ACE") & (mol.name == "C"))[0][0],
                np.where((mol.resname == resn) & (mol.name == "N"))[0][0],
            ],
        )
    )
    mol.bondtype = np.hstack((mol.bondtype, ["1"], ["1"]))
    return mol


def parameterizeNonCanonicalResidues(cifs, outdir, method="gaff2", nnp=None):
    cifs = ensurelist(cifs)
    method = method.lower()
    if method == "ani-2x" and nnp is None:
        raise RuntimeError(
            "The user must provide an NNP calculator to user ANI-2x parameterization"
        )
    if method not in ("gaff2", "ani-2x"):
        raise AttributeError(
            "Parameterization can only be performed with GAFF2 or ANI-2x methods"
        )

    for cif in cifs:
        mol = Molecule(cif)
        _parameterize_non_canonical_residue(mol, outdir, method, nnp=nnp)


def _parameterize_non_canonical_residue(mol, outdir, method, nnp=None):
    try:
        from parameterize.cli import main_parameterize, list_dihedrals
    except ImportError:
        raise ImportError(
            "You are missing the parameterize library. Please install it with conda install parameterize -c acellera -c conda-forge"
        )

    os.makedirs(outdir, exist_ok=True)

    mol = mol.copy()
    # Remove backbone formal charge from templated molecule
    mol.formalcharge[mol.name == "N"] = 0
    resn = mol.resname[0]
    with tempfile.TemporaryDirectory() as tmpdir:
        cmol = _cap_residue(mol)
        cmol.resname[:] = resn  # So that CIF is written as smallmol instead of macromol

        dih = list_dihedrals(cmol, cmol.formalcharge.sum(), _logger=False)
        # Delete pure backbone dihedrals to not reparameterize them and thus waste atomtype names and computation time
        for dd in ("CH3-C-N-CA", "CA-C-N-CH3"):
            if dd in dih:
                del dih[dd]

        main_parameterize(
            cmol,
            user_charge=int(cmol.formalcharge.sum()),
            forcefield="GAFF2",
            charge_type="AM1-BCC",
            min_type="mm",
            dihed_fit_type="iterative",
            dihed_opt_type="mm",
            fit_dihedral=method != "gaff2",
            dihedrals=list(dih.values()),
            nnp=nnp,
            outdir=tmpdir,
        )
        shutil.copy(
            os.path.join(tmpdir, "parameters", "GAFF2", f"{resn}-orig.cif"),
            os.path.join(tmpdir, f"{resn}.cif"),
        )
        shutil.copy(
            os.path.join(tmpdir, "parameters", "GAFF2", f"{resn}.frcmod"),
            os.path.join(outdir, f"{resn}.frcmod"),
        )

        _post_process_parameterize(cmol, tmpdir, outdir, resn)


def _post_process_parameterize(cmol, tmpdir, outdir, resn):
    from subprocess import call
    from collections import defaultdict

    # TODO: Move this to parameterize (?)
    mol = Molecule(os.path.join(tmpdir, f"{resn}.cif"))
    fields = ("element",)
    g1 = makeMolGraph(cmol, "all", fields)
    g2 = makeMolGraph(mol, "all", fields)
    _, _, matching = compareGraphs(
        g1, g2, fields=fields, tolerance=0.5, returnmatching=True
    )
    for pp in matching:  # Rename atoms in reference molecule
        mol.name[pp[0]] = cmol.name[pp[1]]
        mol.formalcharge[pp[0]] = cmol.formalcharge[pp[1]]

    # Rename backbone atom types
    backbone_at = {"N": "N", "H": "H", "CA": "CT", "HA": "H1", "C": "C", "O": "O"}
    sidechain = np.zeros(mol.numAtoms, dtype=bool)
    sidechain[6:-6] = True
    original = defaultdict(list)

    for key, val in backbone_at.items():
        sel = mol.name == key
        for at in mol.atomtype[sel]:
            original[at].append(val)
        mol.atomtype[sel] = val

    frcmod = os.path.join(outdir, f"{resn}.frcmod")
    prm = parmed.amber.AmberParameterSet(frcmod)

    # Duplicate parameters for renamed atoms which don't exist in the backbone
    _duplicate_parameters(prm, original)

    # Remove unused parameters
    _clean_prm(prm, mol, list(backbone_at.values()), sidechain)
    prm.write(frcmod)

    # Remove the caps
    mol.filter(sidechain, _logger=False)

    mol2file = os.path.join(tmpdir, f"{resn}.mol2")
    mol.write(mol2file)

    acfile = os.path.join(tmpdir, f"{resn}_mod.ac")
    call(
        [
            "antechamber",
            "-fi",
            "mol2",
            "-fo",
            "ac",
            "-i",
            mol2file,
            "-o",
            acfile,
            "-dr",
            "n",
            "-j",
            "0",
            "-nc",
            str(mol.formalcharge.sum()),
            "-an",  # adjust atom names
            "n",  # no
            "-pf",
            "y",
        ]
    )

    mainchain = os.path.join(tmpdir, f"mainchain.{resn.lower()}")
    with open(mainchain, "w") as f:
        f.write("HEAD_NAME N\n")
        f.write("TAIL_NAME C\n")
        f.write("MAIN_CHAIN CA\n")
        f.write("PRE_HEAD_TYPE C\n")
        f.write("POST_TAIL_TYPE N\n")
        f.write(f"CHARGE {mol.formalcharge.sum():.1f}\n")

    prepi = os.path.join(outdir, f"{resn}.prepi")
    call(
        [
            "prepgen",
            "-i",
            acfile,
            "-o",
            prepi,
            "-f",
            "prepi",
            "-m",
            mainchain,
            "-rn",
            resn,
        ]
    )
    print("")  # Add a newline after the output of prepgen

    _fix_prepi_atomname_capitalization(mol, prepi)


def _fix_prepi_atomname_capitalization(mol, prepi):
    # Fix wrong prepi atom name capitalization
    uqnames = {x.upper(): x for x in np.unique(mol.name)}

    with open(prepi, "r") as f:
        lines = f.readlines()

    for i in range(len(lines)):
        if lines[i].strip().startswith("CORR"):
            break

    for i in range(i + 2, len(lines)):
        if lines[i].strip() == "":
            break
        old_name = lines[i][6:9].strip()

        # Fix wrong prepi atom name capitalization
        if old_name.upper() in uqnames and uqnames[old_name.upper()] != old_name:
            logger.info(
                f"Fixed residue {mol.resname[0]} atom name {old_name} -> {uqnames[old_name.upper()]} to match the input structure."
            )
            lines[i] = f"{lines[i][:6]}{uqnames[old_name.upper()]:4s}{lines[i][10:]}"

    with open(prepi, "w") as f:
        for line in lines:
            f.write(line)


def _clean_prm(prm, mol, backbone_at, sidechain):
    from moleculekit.util import guessAnglesAndDihedrals

    all_at = np.unique(mol.atomtype[sidechain]).tolist() + backbone_at
    angles, dihedrals = guessAnglesAndDihedrals(mol.bonds)

    at_dict = {
        "bond_types": mol.atomtype[mol.bonds].tolist(),
        "angle_types": mol.atomtype[angles].tolist(),
        "dihedral_types": mol.atomtype[dihedrals].tolist(),
    }

    to_delete = []
    for at in prm.atom_types:
        if at not in all_at:
            to_delete.append(at)
    for at in to_delete:
        del prm.atom_types[at]

    for param_t in ("bond_types", "angle_types", "dihedral_types"):
        to_delete = []
        seen = []
        for bt in getattr(prm, param_t):
            if (
                not all(np.isin(bt, all_at))
                or all(np.isin(bt, backbone_at))
                or (
                    list(bt) not in at_dict[param_t]
                    and list(bt)[::-1] not in at_dict[param_t]
                )
                or list(bt)[::-1] in seen
            ):
                to_delete.append(bt)
            seen.append(list(bt))

        for bt in to_delete:
            del prm.__dict__[param_t][bt]

    for param_t in ("improper_types", "improper_periodic_types"):
        to_delete = []
        for bt in getattr(prm, param_t):
            if not all(np.isin(bt, all_at)) or all(np.isin(bt, backbone_at)):
                to_delete.append(bt)
        for bt in to_delete:
            del prm.__dict__[param_t][bt]


def _duplicate_parameters(prm, original):
    from copy import deepcopy

    def _gen_permutations(typ, original):
        import itertools

        possibles = []
        for tt in typ:
            if tt in original:
                possibles.append(original[tt] + [tt])
            else:
                possibles.append([tt])
        return list(itertools.product(*possibles))

    # Duplicate parameters for renamed atoms which don't exist in the backbone
    for prm_typ in ("bond_types", "angle_types", "dihedral_types", "improper_types"):
        new_bt = {}
        for typ in getattr(prm, prm_typ):
            perms = _gen_permutations(typ, original)
            if len(perms) == 1:
                continue  # No replacements
            for perm in perms:
                new_bt[perm] = getattr(prm, prm_typ)[typ]

        for bt in new_bt:
            prm.__dict__[prm_typ][bt] = deepcopy(new_bt[bt])


# The below is used for testing only
try:
    from parameterize.cli import main_parameterize
except ImportError:
    _PARAMETERIZE_INSTALLED = False
else:
    _PARAMETERIZE_INSTALLED = True


class _TestNCAAResParam(unittest.TestCase):
    @unittest.skipUnless(
        _PARAMETERIZE_INSTALLED, "Can only run with parameterize installed"
    )
    def test_ncaa_residue_parameterization(self):
        from glob import glob

        refdir = home(dataDir=os.path.join("test-custom-residue-param"))
        cifs = glob(os.path.join(refdir, "*.cif"))

        for cif in cifs:
            with tempfile.TemporaryDirectory() as tmpdir, self.subTest(cif):
                parameterizeNonCanonicalResidues(cif, tmpdir, method="gaff2")
                params = glob(os.path.join(tmpdir, "*"))
                assert len(params) == 2


if __name__ == "__main__":
    unittest.main(verbosity=2)
