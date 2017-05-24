import iuv
import utils
import glob
import os
import ast_visitor
from ast_visitor import NondetReplacer,DfsVisitor
from pycparser import c_ast as a
import pycparser
import witness_generation as wit_gen

include_dir = os.path.abspath('./klee/include/')
lib_dir = os.path.abspath('./klee/lib')
bin_dir = os.path.abspath('./klee/bin')
tests_output = utils.tmp
tests_dir = os.path.join(tests_output, 'klee-tests')
klee_make_symbolic = 'klee_make_symbolic'


class InputGenerator(iuv.BaseInputGenerator):

    def __init__(self, timelimit=0, log_verbose=False, search_heuristic=['random-path', 'nurs:covnew'], machine_model='32bit'):
        super().__init__(timelimit, machine_model)
        self.log_verbose = log_verbose
        if type(search_heuristic) is not list:
            self.search_heuristic = list(search_heuristic)
        else:
            self.search_heuristic = search_heuristic

        self._run_env = utils.get_env_with_path_added(bin_dir)

    def get_name(self):
        return 'klee'

    def get_run_env(self):
        return self._run_env

    def create_input_generation_cmds(self, filename):
        compiled_file = '.'.join(os.path.basename(filename).split('.')[:-1] + ['bc'])
        compiled_file = utils.create_file_path(compiled_file, temp_dir=True)
        compile_cmd = ['clang', '-I', include_dir, '-emit-llvm', '-c', '-g', '-o', compiled_file, filename]
        input_generation_cmd = ['klee']
        if self.timelimit > 0:
            input_generation_cmd += ['-max-time', str(self.timelimit)]
        input_generation_cmd.append('-only-output-states-covering-new')
        input_generation_cmd += ['-search=' + h for h in self.search_heuristic]
        input_generation_cmd += ['-output-dir=' + tests_dir]
        input_generation_cmd += [compiled_file]

        return [compile_cmd, input_generation_cmd]

    def _get_var_number(self, test_info_line):
        assert 'object' in test_info_line
        return test_info_line.split(':')[0].split(' ')[-1]  # Object number should be at end, e.g. 'object  1: ...'

    def get_test_vector(self, test):
        ktest_tool = [os.path.join(bin_dir, 'ktest-tool'), '--write-ints']
        exec_output = utils.execute(ktest_tool + [test], log_output=False, quiet=True)
        test_info = exec_output.stdout.split('\n')
        objects = dict()
        for line in [l for l in test_info if l.startswith('object')]:
            if 'name:' in line:
                assert len(line.split(':')) == 3
                var_number = self._get_var_number(line)
                var_name = line.split(':')[2][2:-1]  # [1:-1] to cut the surrounding ''
                if var_number not in objects.keys():
                    objects[var_number] = dict()
                objects[var_number]['name'] = var_name

            elif 'data:' in line:
                assert len(line.split(':')) == 3
                var_number = self._get_var_number(line)
                value = line.split(':')[-1].strip()
                objects[var_number]['value'] = value

        return objects

    def create_witness(self, filename, test_file):
        witness = wit_gen.create_witness(producer=self.get_name(),
                                         filename=filename,
                                         test_vector=self.get_test_vector(test_file),
                                         nondet_var_map=self.get_nondet_var_map(filename),
                                         machine_model=self.machine_model)

        test_name = '.'.join(os.path.basename(test_file).split('.')[:-1])
        witness_file = test_name + ".witness.graphml"
        witness_file = utils.create_file_path(witness_file, temp_dir=True)

        return {'name': witness_file, 'content': witness}

    def create_all_witnesses(self, filename):
        witnesses = []
        for test in glob.iglob(tests_dir + '/*.ktest'):
            witness = self.create_witness(filename, test)
            witnesses.append(witness)
        return witnesses

    def get_ast_replacer(self):
        return AstReplacer()

    def create_nondet_var_map(self, filename):
        visitor = NondetIdentifierCollector()
        ast = pycparser.parse_file(filename)
        visitor.visit(ast)
        return visitor.nondet_identifiers


class AstReplacer(NondetReplacer):

    def _get_amper(self, var_name):
        return a.UnaryOp('&', a.ID(var_name))

    def _get_sizeof_call(self, var_name):
        return a.UnaryOp('sizeof', a.ID(var_name))

    def _get_string(self, string):
        return a.Constant('string', '\"' + string + '\"')

    # Hook
    def get_nondet_marker(self, var_name, var_type):
        parameters = [self._get_amper(var_name), self._get_sizeof_call(var_name), self._get_string(var_name)]
        return a.FuncCall(a.ID(klee_make_symbolic), a.ExprList(parameters))

    # Hook
    def get_error_stmt(self):
        parameters = [a.Constant('int', str(utils.error_return))]
        return a.FuncCall(a.ID('exit'), a.ExprList(parameters))

    # Hook
    def get_preamble(self):
        parser = pycparser.CParser()
        # Define dummy klee_make_symbolic
        definitions = 'typedef unsigned long int size_t;'
        make_symbolic_def = 'void klee_make_symbolic(void *addr, size_t type, const char *name) { }'
        full_preamble = '\n'.join([definitions, make_symbolic_def])
        ast = parser.parse(full_preamble)
        return ast.ext


class NondetIdentifierCollector(DfsVisitor):

    def __init__(self):
        super().__init__()
        self.nondet_identifiers = dict()
        self.scope = list()

    def visit_FuncCall(self, item):
        func_name = ast_visitor.get_name(item)
        if func_name != klee_make_symbolic:
            return []
        relevant_var = item.args.exprs[2].value[1:-1]  # cut surrounding ""

        self.nondet_identifiers[relevant_var] = {'line': item.coord.line,
                                                 'origin file': item.coord.file,
                                                 'scope': self.scope[-1]}
        # no need to visit item.args, we don't do nested klee_make_symbolic calls
        return []

    def visit_FuncDef(self, item):
        self.scope.append(ast_visitor.get_name(item.decl))
        self.visit(item.body)
        self.scope = self.scope[:-1]

        return []

