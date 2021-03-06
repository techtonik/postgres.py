from __future__ import absolute_import, division, print_function, unicode_literals

import os
from collections import namedtuple
from unittest import TestCase

from postgres import Postgres, NotAModel, NotRegistered
from postgres.cursors import TooFew, TooMany, SimpleDictCursor
from postgres.orm import ReadOnly, Model
from psycopg2 import InterfaceError, ProgrammingError
from pytest import raises


DATABASE_URL = os.environ['DATABASE_URL']


# harnesses
# =========

class WithSchema(TestCase):

    def setUp(self):
        self.db = Postgres(DATABASE_URL, cursor_factory=SimpleDictCursor)
        self.db.run("DROP SCHEMA IF EXISTS public CASCADE")
        self.db.run("CREATE SCHEMA public")

    def tearDown(self):
        self.db.run("DROP SCHEMA IF EXISTS public CASCADE")
        del self.db


class WithData(WithSchema):

    def setUp(self):
        WithSchema.setUp(self)
        self.db.run("CREATE TABLE foo (bar text)")
        self.db.run("INSERT INTO foo VALUES ('baz')")
        self.db.run("INSERT INTO foo VALUES ('buz')")


# db.run
# ======

class TestRun(WithSchema):

    def test_run_runs(self):
        self.db.run("CREATE TABLE foo (bar text)")
        actual = self.db.all("SELECT tablename FROM pg_tables "
                             "WHERE schemaname='public'")
        assert actual == ["foo"]

    def test_run_inserts(self):
        self.db.run("CREATE TABLE foo (bar text)")
        self.db.run("INSERT INTO foo VALUES ('baz')")
        actual = self.db.one("SELECT * FROM foo ORDER BY bar")
        assert actual == "baz"


# db.all
# ======

class TestRows(WithData):

    def test_rows_fetches_all_rows(self):
        actual = self.db.all("SELECT * FROM foo ORDER BY bar")
        assert actual == ["baz", "buz"]

    def test_rows_fetches_one_row(self):
        actual = self.db.all("SELECT * FROM foo WHERE bar='baz'")
        assert actual == ["baz"]

    def test_rows_fetches_no_rows(self):
        actual = self.db.all("SELECT * FROM foo WHERE bar='blam'")
        assert actual == []

    def test_bind_parameters_as_dict_work(self):
        params = {"bar": "baz"}
        actual = self.db.all("SELECT * FROM foo WHERE bar=%(bar)s", params)
        assert actual == ["baz"]

    def test_bind_parameters_as_tuple_work(self):
        actual = self.db.all("SELECT * FROM foo WHERE bar=%s", ("baz",))
        assert actual == ["baz"]


# db.one
# ======

class TestWrongNumberException(WithData):

    def test_TooFew_message_is_helpful(self):
        try:
            actual = self.db.one("CREATE TABLE foux (baar text)")
        except TooFew as exc:
            actual = str(exc)
        assert actual == "Got -1 rows; expecting 0 or 1."

    def test_TooMany_message_is_helpful_for_two_options(self):
        try:
            with self.db.get_cursor() as cursor:
                actual = cursor._some( "SELECT * FROM foo"
                                     , parameters=None
                                     , lo=1
                                     , hi=1
                                      )
        except TooMany as exc:
            actual = str(exc)
        assert actual == "Got 2 rows; expecting exactly 1."

    def test_TooMany_message_is_helpful_for_a_range(self):
        self.db.run("INSERT INTO foo VALUES ('blam')")
        self.db.run("INSERT INTO foo VALUES ('blim')")
        try:
            with self.db.get_cursor() as cursor:
                actual = cursor._some( "SELECT * FROM foo"
                                     , parameters=None
                                     , lo=1
                                     , hi=3
                                      )
        except TooMany as exc:
            actual = str(exc)
        assert actual == "Got 4 rows; expecting between 1 and 3 (inclusive)."


class TestOneOrZero(WithData):

    def test_one_raises_TooFew(self):
        self.assertRaises( TooFew
                         , self.db.one
                         , "CREATE TABLE foux (baar text)"
                          )

    def test_one_rollsback_on_error(self):
        try:
            self.db.one("CREATE TABLE foux (baar text)")
        except TooFew:
            pass
        self.assertRaises( ProgrammingError
                         , self.db.all
                         , "SELECT * FROM foux"
                          )

    def test_one_returns_None(self):
        actual = self.db.one("SELECT * FROM foo WHERE bar='blam'")
        assert actual is None

    def test_one_returns_whatever(self):
        class WHEEEE: pass
        actual = self.db.one( "SELECT * FROM foo WHERE bar='blam'"
                            , default=WHEEEE
                             )
        assert actual is WHEEEE

    def test_one_returns_one(self):
        actual = self.db.one("SELECT * FROM foo WHERE bar='baz'")
        assert actual == "baz"

    def test_with_strict_True_one_raises_TooMany(self):
        self.assertRaises(TooMany, self.db.one, "SELECT * FROM foo")


# db.get_cursor
# =============

class TestCursor(WithData):

    def test_get_cursor_gets_a_cursor(self):
        with self.db.get_cursor() as cursor:
            cursor.execute("INSERT INTO foo VALUES ('blam')")
            cursor.execute("SELECT * FROM foo ORDER BY bar")
            actual = cursor.fetchall()
        assert actual == [{"bar": "baz"}, {"bar": "blam"}, {"bar": "buz"}]

    def test_transaction_is_isolated(self):
        with self.db.get_cursor() as cursor:
            cursor.execute("INSERT INTO foo VALUES ('blam')")
            cursor.execute("SELECT * FROM foo ORDER BY bar")
            actual = self.db.all("SELECT * FROM foo ORDER BY bar")
        assert actual == ["baz", "buz"]

    def test_transaction_commits_on_success(self):
        with self.db.get_cursor() as cursor:
            cursor.execute("INSERT INTO foo VALUES ('blam')")
            cursor.execute("SELECT * FROM foo ORDER BY bar")
        actual = self.db.all("SELECT * FROM foo ORDER BY bar")
        assert actual == ["baz", "blam", "buz"]

    def test_transaction_rolls_back_on_failure(self):
        class Heck(Exception): pass
        try:
            with self.db.get_cursor() as cursor:
                cursor.execute("INSERT INTO foo VALUES ('blam')")
                cursor.execute("SELECT * FROM foo ORDER BY bar")
                raise Heck
        except Heck:
            pass
        actual = self.db.all("SELECT * FROM foo ORDER BY bar")
        assert actual == ["baz", "buz"]

    def test_we_close_the_cursor(self):
        with self.db.get_cursor() as cursor:
            cursor.execute("SELECT * FROM foo ORDER BY bar")
        self.assertRaises( InterfaceError
                         , cursor.fetchall
                          )

    def test_monkey_patch_execute(self):
        expected = "SELECT 1"
        def execute(this, sql, params=[]):
            return sql
        from postgres.cursors import SimpleCursorBase
        SimpleCursorBase.execute = execute
        with self.db.get_cursor() as cursor:
            actual = cursor.execute(expected)
        del SimpleCursorBase.execute
        assert actual == expected


# db.get_connection
# =================

class TestConnection(WithData):

    def test_get_connection_gets_a_connection(self):
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM foo ORDER BY bar")
            actual = cursor.fetchall()
        assert actual == [{"bar": "baz"}, {"bar": "buz"}]


# orm
# ===

class TestORM(WithData):

    class MyModel(Model):

        typname = "foo"

        def __init__(self, record):
            Model.__init__(self, record)
            self.bar_from_init = record['bar']

        def update_bar(self, bar):
            self.db.run( "UPDATE foo SET bar=%s WHERE bar=%s"
                       , (bar, self.bar)
                        )
            self.set_attributes(bar=bar)

    def setUp(self):
        WithData.setUp(self)
        self.db.register_model(self.MyModel)

    def tearDown(self):
        self.db.model_registry = {}

    def installFlah(self):
        self.db.run("CREATE TABLE flah (bar text)")
        self.db.register_model(self.MyModel, 'flah')

    def test_orm_basically_works(self):
        one = self.db.one("SELECT foo.*::foo FROM foo WHERE bar='baz'")
        assert one.__class__ == self.MyModel

    def test_orm_models_get_kwargs_to_init(self):
        one = self.db.one("SELECT foo.*::foo FROM foo WHERE bar='baz'")
        assert one.bar_from_init == 'baz'

    def test_updating_attributes_works(self):
        one = self.db.one("SELECT foo.*::foo FROM foo WHERE bar='baz'")
        one.update_bar("blah")
        bar = self.db.one("SELECT bar FROM foo WHERE bar='blah'")
        assert bar == one.bar

    def test_attributes_are_read_only(self):
        one = self.db.one("SELECT foo.*::foo FROM foo WHERE bar='baz'")
        def assign():
            one.bar = "blah"
        self.assertRaises(ReadOnly, assign)

    def test_check_register_raises_if_passed_a_model_instance(self):
        obj = self.MyModel({'bar': 'baz'})
        raises(NotAModel, self.db.check_registration, obj)

    def test_check_register_doesnt_include_subsubclasses(self):
        class Other(self.MyModel): pass
        raises(NotRegistered, self.db.check_registration, Other)

    def test_dot_dot_dot_unless_you_ask_it_to(self):
        class Other(self.MyModel): pass
        assert self.db.check_registration(Other, True) == 'foo'

    def test_check_register_handles_complex_cases(self):
        self.installFlah()

        class Second(Model): pass
        self.db.run("CREATE TABLE blum (bar text)")
        self.db.register_model(Second, 'blum')
        assert self.db.check_registration(Second) == 'blum'

        class Third(self.MyModel, Second): pass
        actual = list(sorted(self.db.check_registration(Third, True)))
        assert actual == ['blum', 'flah', 'foo']

    def test_a_model_can_be_used_for_a_second_type(self):
        self.installFlah()
        self.db.run("INSERT INTO flah VALUES ('double')")
        self.db.run("INSERT INTO flah VALUES ('trouble')")
        flah = self.db.one("SELECT flah.*::flah FROM flah WHERE bar='double'")
        assert flah.bar == "double"

    def test_check_register_returns_string_for_single(self):
        assert self.db.check_registration(self.MyModel) == 'foo'

    def test_check_register_returns_list_for_multiple(self):
        self.installFlah()
        actual = list(sorted(self.db.check_registration(self.MyModel)))
        assert actual == ['flah', 'foo']

    def test_unregister_unregisters_one(self):
        self.db.unregister_model(self.MyModel)
        assert self.db.model_registry == {}

    def test_unregister_leaves_other(self):
        self.db.run("CREATE TABLE flum (bar text)")
        class OtherModel(Model): pass
        self.db.register_model(OtherModel, 'flum')
        self.db.unregister_model(self.MyModel)
        assert self.db.model_registry == {'flum': OtherModel}

    def test_unregister_unregisters_multiple(self):
        self.installFlah()
        self.db.unregister_model(self.MyModel)
        assert self.db.model_registry == {}


# cursor_factory
# ==============

class TestCursorFactory(WithData):

    def setUp(self):                    # override
        self.db = Postgres(DATABASE_URL)
        self.db.run("DROP SCHEMA IF EXISTS public CASCADE")
        self.db.run("CREATE SCHEMA public")
        self.db.run("CREATE TABLE foo (bar text, baz int)")
        self.db.run("INSERT INTO foo VALUES ('buz', 42)")
        self.db.run("INSERT INTO foo VALUES ('biz', 43)")

    def test_NamedDictCursor_results_in_namedtuples(self):
        Record = namedtuple("Record", ["bar", "baz"])
        expected = [Record(bar="biz", baz=43), Record(bar="buz", baz=42)]
        actual = self.db.all("SELECT * FROM foo ORDER BY bar")
        assert actual == expected

    def test_namedtuples_can_be_unrolled(self):
        actual = self.db.all("SELECT baz FROM foo ORDER BY bar")
        assert actual == [43, 42]
